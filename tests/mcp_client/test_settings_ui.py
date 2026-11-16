# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Settings > Integrations in-place MCP server editor and live probe."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from plugin.mcp import client as mcp_client
from plugin.mcp_client.settings_ui import McpServersEditor
from tests.mcp.test_client import FakeMcpServer, TOKEN, TOOLS


class _FakeCombo:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.items: list[str] = []

    def getText(self) -> str:
        return self.text

    def setText(self, t: str) -> None:
        self.text = t

    def getItemCount(self) -> int:
        return len(self.items)

    def getItem(self, i: int) -> str:
        return self.items[i]

    def removeItems(self, pos: int, count: int) -> None:
        del self.items[pos : pos + count]

    def addItems(self, items: Any, pos: int) -> None:
        self.items[pos:pos] = list(items)


class _FakeText:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def getText(self) -> str:
        return self.text

    def setText(self, t: str) -> None:
        self.text = t


class _FakeCheck:
    def __init__(self, state: int = 0) -> None:
        self.state = state

    def getState(self) -> int:
        return self.state

    def setState(self, s: int) -> None:
        self.state = 1 if s else 0


class _FakeButton:
    pass


def _make_controls() -> dict[str, Any]:
    return {
        "server": _FakeCombo(),
        "add_server": _FakeButton(),
        "remove_server": _FakeButton(),
        "enabled": _FakeCheck(),
        "trusted": _FakeCheck(),
        "name": _FakeText(),
        "url": _FakeText(),
        "token_keychain": _FakeText(),
        "token": _FakeText(),
        "status": _FakeText(),
    }


@pytest.fixture(autouse=True)
def _clear_mcp_connections():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


@pytest.fixture
def fake():
    srv = FakeMcpServer()
    try:
        yield srv
    finally:
        srv.close()


def _run_inline(fn: Any, *args: Any, **kwargs: Any) -> None:
    fn(*args)


def test_add_fill_save_writes_servers_and_keeps_unknown_keys():
    stored: dict[str, Any] = {
        "existing": {"url": "http://localhost:9000/mcp", "headers": {"X-Custom": "1"}, "timeout": 120}
    }
    saved: list[dict[str, Any]] = []

    def fake_save(patch_dict: dict[str, Any]) -> None:
        saved.append(patch_dict)
        stored.update(patch_dict.get("mcp_client.servers", {}))

    ctrls = _make_controls()
    editor = McpServersEditor(ctrls, stored)
    editor.load("existing")

    with (
        patch("plugin.mcp_client.settings_ui.get_config", side_effect=lambda k: stored if k == "mcp_client.servers" else None),
        patch("plugin.mcp_client.settings_ui.config.save", side_effect=fake_save),
    ):
        editor.add()
        assert ctrls["enabled"].getState() == 1
        ctrls["name"].setText("home")
        ctrls["url"].setText("http://localhost:8000/mcp")
        ctrls["token"].setText("secret-tok")
        ctrls["trusted"].setState(1)
        editor.save()

    assert len(saved) == 1
    out = saved[0]["mcp_client.servers"]
    assert out["existing"]["headers"] == {"X-Custom": "1"}
    assert out["existing"]["timeout"] == 120
    assert out["home"]["url"] == "http://localhost:8000/mcp"
    assert out["home"]["token"] == "secret-tok"
    assert out["home"]["trust"] == "full"
    assert out["home"]["enabled"] is True


def test_rename_moves_entry_and_rejects_taken_name():
    ctrls = _make_controls()
    editor = McpServersEditor(
        ctrls,
        {"alpha": {"url": "http://a/mcp"}, "beta": {"url": "http://b/mcp"}},
    )
    editor.load("alpha")
    ctrls["name"].setText("gamma")
    editor.commit()
    assert "gamma" in editor._servers
    assert "alpha" not in editor._servers
    assert ctrls["server"].items == ["beta", "gamma"]

    ctrls["name"].setText("beta")
    editor.commit()
    assert "gamma" in editor._servers
    assert "already exists" in ctrls["status"].getText()


def test_remove_and_blank_added_entry_not_saved():
    saved: list[dict[str, Any]] = []
    ctrls = _make_controls()
    editor = McpServersEditor(ctrls, {"alpha": {"url": "http://a/mcp"}})
    editor.load("alpha")
    editor.remove()
    assert editor._servers == {}
    assert ctrls["name"].getText() == ""

    editor.add()
    assert "server" in editor._servers
    with (
        patch("plugin.mcp_client.settings_ui.get_config", return_value={"alpha": {"url": "http://a/mcp"}}),
        patch("plugin.mcp_client.settings_ui.config.save", side_effect=saved.append),
    ):
        editor.save()
    assert saved == [{"mcp_client.servers": {}}]


def test_probe_success_dedup_and_auth_failure(fake):
    ctrls = _make_controls()
    editor = McpServersEditor(ctrls, {})
    editor.load(None)
    ctrls["name"].setText("claw")
    ctrls["url"].setText(fake.url)
    ctrls["token"].setText(TOKEN)

    with (
        patch("plugin.mcp_client.settings_ui.run_in_background", side_effect=_run_inline),
        patch("plugin.mcp_client.settings_ui.main.post", side_effect=lambda fn: fn()),
    ):
        editor.probe_if_changed()
        assert ctrls["status"].getText() == f"Connected: {len(TOOLS)} tools"
        assert fake.methods().count("tools/list") == 1

        # Same URL/token/keychain triple does not probe again.
        editor.probe_if_changed()
        assert fake.methods().count("tools/list") == 1

        # Wrong token reports 401 without leaking the bad token.
        bad_token = "wrong-secret-token-value"
        ctrls["token"].setText(bad_token)
        editor.probe_if_changed()
        status = ctrls["status"].getText()
        assert status.startswith("Failed:")
        assert "401" in status
        assert bad_token not in status


def test_stale_generation_does_not_overwrite_status():
    ctrls = _make_controls()
    editor = McpServersEditor(ctrls, {"claw": {"url": "http://localhost:8000/mcp"}})
    editor.load("claw")
    editor._pending["claw"] = 2
    editor._apply("claw", 1, {"name": "claw"}, "Connected: 1 tools", True)
    assert ctrls["status"].getText() == ""


def test_close_prevents_apply_from_writing():
    ctrls = _make_controls()
    editor = McpServersEditor(ctrls, {"claw": {"url": "http://localhost:8000/mcp"}})
    editor.load("claw")
    editor._pending["claw"] = 1
    editor.close()
    editor._apply("claw", 1, {"name": "claw"}, "Connected: 1 tools", True)
    assert ctrls["status"].getText() == ""


def test_save_keeps_probed_cache_when_unchanged(fake):
    stored: dict[str, Any] = {}
    ctrls = _make_controls()
    editor = McpServersEditor(ctrls, {})
    editor.load(None)
    ctrls["name"].setText("claw")
    ctrls["url"].setText(fake.url)
    ctrls["token"].setText(TOKEN)

    with (
        patch("plugin.mcp_client.settings_ui.run_in_background", side_effect=_run_inline),
        patch("plugin.mcp_client.settings_ui.main.post", side_effect=lambda fn: fn()),
    ):
        editor.probe_if_changed()

    def fake_save(patch_dict: dict[str, Any]) -> None:
        stored.clear()
        stored.update(patch_dict["mcp_client.servers"])

    with (
        patch("plugin.mcp_client.settings_ui.get_config", side_effect=lambda k: stored if k == "mcp_client.servers" else None),
        patch("plugin.mcp_client.settings_ui.config.save", side_effect=fake_save),
    ):
        keep = editor.save()

    assert len(keep) == 1
    mcp_client.drop_connections(keep=keep)

    chat_ctx = SimpleNamespace(
        caller="chat",
        doc_type="writer",
        send_cancellation=None,
        status_callback=None,
        approval_callback=None,
        chat_append_callback=None,
    )
    with patch.object(mcp_client, "get_config", side_effect=lambda k: stored if k == "mcp_client.servers" else None):
        tools, _hint = mcp_client.mcp_domain_tools(chat_ctx)

    assert len(tools) == len(TOOLS)
    assert fake.methods().count("tools/list") == 1
