# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``plugin/mcp_client/settings_ui.py`` (in-place server editor and live probe)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework import config as fw_config
from plugin.framework import keystore
from plugin.mcp_client.client import McpClientError, McpServerConnection
from plugin.mcp_client import settings_ui
from plugin.mcp_client.settings_ui import McpServersEditor


class _FakeText:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.focus_listeners: list[Any] = []

    def getText(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text

    def addFocusListener(self, listener: Any) -> None:
        self.focus_listeners.append(listener)


class _FakeCheck:
    def __init__(self, state: int = 0) -> None:
        self._state = state

    def getState(self) -> int:
        return self._state

    def setState(self, state: int) -> None:
        self._state = state


class _FakeButton:
    def __init__(self) -> None:
        self.action_listeners: list[Any] = []

    def addActionListener(self, listener: Any) -> None:
        self.action_listeners.append(listener)


class _FakeCombo(_FakeText):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[str] = []
        self.item_listeners: list[Any] = []

    def getItemCount(self) -> int:
        return len(self.items)

    def removeItems(self, pos: int, count: int) -> None:
        del self.items[pos : pos + count]

    def addItems(self, items: tuple[str, ...] | list[str], pos: int) -> None:
        self.items[pos:pos] = list(items)

    def addItemListener(self, listener: Any) -> None:
        self.item_listeners.append(listener)


class _FakeDlg:
    def __init__(self) -> None:
        self.controls: dict[str, Any] = {
            "mcp_client__server": _FakeCombo(),
            "mcp_client__add_server": _FakeButton(),
            "mcp_client__remove_server": _FakeButton(),
            "mcp_client__enabled": _FakeCheck(),
            "mcp_client__trusted": _FakeCheck(),
            "mcp_client__name": _FakeText(),
            "mcp_client__url": _FakeText(),
            "mcp_client__token": _FakeText(),
            "mcp_client__status": _FakeText(),
        }

    def getControl(self, name: str) -> Any:
        return self.controls.get(name)


def _attach_editor(
    monkeypatch: pytest.MonkeyPatch,
    initial_servers: dict[str, Any] | None = None,
) -> tuple[McpServersEditor, _FakeDlg]:
    dlg = _FakeDlg()
    data = dict(initial_servers) if initial_servers is not None else {}
    monkeypatch.setattr(settings_ui, "get_config", lambda key: data if key == "mcp_client.servers" else None)
    monkeypatch.setattr(settings_ui.main, "post", lambda fn: fn())

    attached: list[Any] = []

    def _attach(ctrl: Any, kind: str, listener: Any) -> None:
        getattr(ctrl, f"add{kind}Listener")(listener)
        attached.append((ctrl, kind, listener))

    editor = McpServersEditor.attach(dlg, _attach)
    assert editor is not None
    return editor, dlg


def test_add_fill_save_writes_servers_and_routes_token_to_keystore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_keystore: Any,
) -> None:
    cfg_file = tmp_path / "writeragent.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fw_config, "get_config_file_path", lambda: str(cfg_file))

    editor, dlg = _attach_editor(monkeypatch, {})
    editor.add()
    assert dlg.controls["mcp_client__server"].getText() == ""
    dlg.controls["mcp_client__name"].setText("home")
    dlg.controls["mcp_client__url"].setText("http://127.0.0.1:8000/mcp")
    dlg.controls["mcp_client__token"].setText("tok-secret-1")

    monkeypatch.setattr(settings_ui, "run_in_background", lambda *_a, **_kw: None)
    editor.probe_if_changed()
    assert "home" in dlg.controls["mcp_client__server"].items

    editor.save()

    raw_file = cfg_file.read_text(encoding="utf-8")
    assert "tok-secret-1" not in raw_file
    saved = fw_config.parse_config_json_text(raw_file)["mcp_client.servers"]
    assert set(saved) == {"home"}
    assert saved["home"]["url"] == "http://127.0.0.1:8000/mcp"
    assert keystore.is_ref(saved["home"]["token"])
    assert fw_config.resolve_entry_secret("mcp_client.servers", "home", "token", saved["home"]["token"]) == "tok-secret-1"


def test_untouched_form_and_unknown_keys_preserve_config(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = {
        "home": {"url": "http://localhost:8000/mcp", "headers": {"X-Custom": "1"}, "timeout": 120},
        "junk": "not-a-dict",
    }
    editor, dlg = _attach_editor(monkeypatch, initial)
    assert dlg.controls["mcp_client__server"].items == ["home"]

    save_mock = MagicMock()
    monkeypatch.setattr(settings_ui.config, "save", save_mock)
    editor.save()
    save_mock.assert_not_called()

    # Editing URL preserves unknown keys and non-dict entries.
    dlg.controls["mcp_client__url"].setText("http://localhost:9000/mcp")
    editor.save()
    save_mock.assert_called_once()
    saved = save_mock.call_args[0][0]["mcp_client.servers"]
    assert saved["home"]["headers"] == {"X-Custom": "1"}
    assert saved["home"]["timeout"] == 120
    assert saved["junk"] == "not-a-dict"


def test_remove_deletes_entry_and_rename_keeps_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_keystore: Any,
) -> None:
    cfg_file = tmp_path / "writeragent.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fw_config, "get_config_file_path", lambda: str(cfg_file))
    fw_config.save({"mcp_client.servers": {"a": {"url": "http://a/mcp", "token": "tok-a"}, "b": {"url": "http://b/mcp"}}})
    monkeypatch.setattr(settings_ui, "get_config", fw_config.get_config)

    dlg = _FakeDlg()
    monkeypatch.setattr(settings_ui.main, "post", lambda fn: fn())
    editor = McpServersEditor.attach(dlg, lambda ctrl, kind, listener: getattr(ctrl, f"add{kind}Listener")(listener))
    assert editor is not None

    # Rename 'a' -> 'alpha'
    dlg.controls["mcp_client__name"].setText("alpha")
    editor.select("b")
    editor.remove()
    editor.save()

    saved = fw_config.get_config("mcp_client.servers")
    assert set(saved) == {"alpha"}
    assert fw_config.resolve_entry_secret("mcp_client.servers", "alpha", "token", saved["alpha"]["token"]) == "tok-a"


def test_unreadable_ref_keeps_ref_and_blocks_rename(
    monkeypatch: pytest.MonkeyPatch,
    fake_keystore: Any,
) -> None:
    from plugin.chatbot import settings_fields

    monkeypatch.setattr(settings_fields, "run_in_background", lambda fn, *_a, **_kw: fn())
    monkeypatch.setattr(settings_fields.main, "post", lambda fn: fn())
    fake_keystore.locked = True
    initial = {"claw": {"url": "http://claw/mcp", "token": "keystore:deadbeef"}}
    editor, dlg = _attach_editor(monkeypatch, initial)
    assert dlg.controls["mcp_client__token"].getText() == "keystore:deadbeef"
    assert "keyring" in dlg.controls["mcp_client__status"].getText().lower()

    # Attempt rename while unreadable: refused.
    save_mock = MagicMock()
    monkeypatch.setattr(settings_ui.config, "save", save_mock)
    dlg.controls["mcp_client__name"].setText("claw2")
    editor.save()
    assert "Unlock the keyring" in dlg.controls["mcp_client__status"].getText()
    save_mock.assert_not_called()

    # A later valid URL edit still preserves the raw ref in the saved patch.
    dlg.controls["mcp_client__name"].setText("claw")
    dlg.controls["mcp_client__url"].setText("http://claw/mcp2")
    editor.save()
    save_mock.assert_called_once()
    saved = save_mock.call_args[0][0]["mcp_client.servers"]
    assert set(saved) == {"claw"}
    assert saved["claw"]["token"] == "keystore:deadbeef"


def test_clearing_missing_token_removes_ref_and_allows_rename(
    monkeypatch: pytest.MonkeyPatch,
    fake_keystore: Any,
) -> None:
    from pathlib import Path
    from plugin.framework import config as fw_config

    fw_config.save({"mcp_client.servers": {"claw": {"url": "http://claw/mcp", "token": "tok-1"}}})
    ref = fw_config.get_config("mcp_client.servers")["claw"]["token"]
    fake_keystore.store.clear()
    keystore_mod = settings_ui.keystore
    keystore_mod.reset_for_tests(fake_keystore)

    from plugin.chatbot import settings_fields

    monkeypatch.setattr(settings_fields, "run_in_background", lambda fn, *_a, **_kw: fn())
    monkeypatch.setattr(settings_fields.main, "post", lambda fn: fn())

    editor, dlg = _attach_editor(monkeypatch, fw_config.get_config("mcp_client.servers"))
    assert dlg.controls["mcp_client__token"].getText() == ref

    dlg.controls["mcp_client__token"].setText("")
    dlg.controls["mcp_client__name"].setText("claw2")
    editor.save()

    saved = fw_config.get_config("mcp_client.servers")
    assert set(saved) == {"claw2"}
    assert "token" not in saved["claw2"]
    assert ref not in Path(fw_config.get_config_file_path()).read_text(encoding="utf-8")


def test_add_with_existing_name_saves_no_new_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = {"home": {"url": "http://localhost:8000/mcp"}}
    editor, dlg = _attach_editor(monkeypatch, initial)
    editor.add()
    assert dlg.controls["mcp_client__server"].getText() == ""
    dlg.controls["mcp_client__name"].setText("home")
    dlg.controls["mcp_client__url"].setText("http://localhost:9000/mcp")

    save_mock = MagicMock()
    monkeypatch.setattr(settings_ui.config, "save", save_mock)
    editor.save()
    assert "already exists" in dlg.controls["mcp_client__status"].getText()
    save_mock.assert_not_called()


def test_probe_starts_once_per_change_and_empty_url_clears_status(monkeypatch: pytest.MonkeyPatch) -> None:
    editor, dlg = _attach_editor(monkeypatch, {"home": {"url": "http://127.0.0.1:8000/mcp"}})
    jobs: list[tuple[Any, tuple[Any, ...]]] = []
    monkeypatch.setattr(settings_ui, "run_in_background", lambda fn, *args, **_kw: jobs.append((fn, args)))

    # Repeated focus-lost with no change starts nothing.
    editor.probe_if_changed()
    assert jobs == []

    # URL change starts exactly one check.
    dlg.controls["mcp_client__url"].setText("http://127.0.0.1:9000/mcp")
    editor.probe_if_changed()
    editor.probe_if_changed()
    assert len(jobs) == 1
    assert dlg.controls["mcp_client__status"].getText() == "Checking…"

    fake_conn = MagicMock(spec=McpServerConnection)
    fake_conn.list_tools.return_value = [{"name": "t1"}, {"name": "t2"}]
    with patch.object(settings_ui, "probe_server", return_value=fake_conn):
        fn, args = jobs[0]
        fn(*args)
    assert dlg.controls["mcp_client__status"].getText() == "Connected: 2 tools"

    # Empty URL clears status and starts no check.
    dlg.controls["mcp_client__url"].setText("")
    editor.probe_if_changed()
    assert len(jobs) == 1
    assert dlg.controls["mcp_client__status"].getText() == ""


def test_probe_failure_and_stale_generation_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    editor, dlg = _attach_editor(monkeypatch, {"home": {"url": "http://127.0.0.1:8000/mcp"}})
    jobs: list[tuple[Any, tuple[Any, ...]]] = []
    monkeypatch.setattr(settings_ui, "run_in_background", lambda fn, *args, **_kw: jobs.append((fn, args)))

    dlg.controls["mcp_client__url"].setText("http://127.0.0.1:8001/mcp")
    editor.probe_if_changed()
    cancel_a = jobs[0][1][3]

    dlg.controls["mcp_client__url"].setText("http://127.0.0.1:8002/mcp")
    editor.probe_if_changed()
    assert cancel_a.is_cancelled()

    # Deliver stale A -> status remains Checking…
    conn_a = MagicMock(spec=McpServerConnection)
    conn_a.list_tools.return_value = [{"name": "a"}]
    with patch.object(settings_ui, "probe_server", return_value=conn_a):
        jobs[0][0](*jobs[0][1])
    assert dlg.controls["mcp_client__status"].getText() == "Checking…"

    # Deliver B failure -> shows Failed: <msg>
    with patch.object(settings_ui, "probe_server", side_effect=McpClientError("refused")):
        jobs[1][0](*jobs[1][1])
    assert dlg.controls["mcp_client__status"].getText() == "Failed: refused"


def test_close_cancels_inflight_and_save_adopts_only_unchanged_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    editor, dlg = _attach_editor(monkeypatch, {"home": {"url": "http://127.0.0.1:8000/mcp"}})
    jobs: list[tuple[Any, tuple[Any, ...]]] = []
    monkeypatch.setattr(settings_ui, "run_in_background", lambda fn, *args, **_kw: jobs.append((fn, args)))
    monkeypatch.setattr(settings_ui.config, "save", lambda _patch: None)

    dlg.controls["mcp_client__url"].setText("http://127.0.0.1:8001/mcp")
    editor.probe_if_changed()
    conn = MagicMock(spec=McpServerConnection)
    conn.list_tools.return_value = [{"name": "t1"}]
    with patch.object(settings_ui, "probe_server", return_value=conn):
        jobs[0][0](*jobs[0][1])

    # Unchanged since probe -> save returns [conn].
    assert editor.save() == [conn]

    # Edit after probe -> save returns [].
    dlg.controls["mcp_client__token"].setText("new-tok")
    assert editor.save() == []

    # Start another probe, then close() -> cancels and ignores late post.
    editor.probe_if_changed()
    cancel_b = jobs[1][1][3]
    editor.close()
    assert cancel_b.is_cancelled()
    with patch.object(settings_ui, "probe_server", return_value=conn):
        jobs[1][0](*jobs[1][1])
    assert dlg.controls["mcp_client__status"].getText() == "Checking…"


def test_commit_strips_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    editor, dlg = _attach_editor(monkeypatch, {})
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(settings_ui.config, "save", lambda patch: saved.append(patch))

    editor.add()
    dlg.controls["mcp_client__name"].setText("home")
    dlg.controls["mcp_client__url"].setText("  http://127.0.0.1:8000/mcp \n")
    dlg.controls["mcp_client__token"].setText(" tok ")
    editor.save()

    assert saved == [
        {
            "mcp_client.servers": {
                "home": {
                    "url": "http://127.0.0.1:8000/mcp",
                    "token": "tok",
                }
            }
        }
    ]


def test_async_token_read_fills_field_without_focus_probe(
    monkeypatch: pytest.MonkeyPatch,
    fake_keystore: Any,
) -> None:
    from plugin.chatbot import settings_fields
    from plugin.framework import config as fw_config, keystore

    fw_config.save({"mcp_client.servers": {"home": {"url": "http://127.0.0.1:8000/mcp", "token": "tok-async"}}})
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.get_calls = 0

    workers: list[Any] = []
    monkeypatch.setattr(settings_fields, "run_in_background", lambda fn, *_a, **_kw: workers.append(fn))
    monkeypatch.setattr(settings_fields.main, "post", lambda fn: fn())

    editor, dlg = _attach_editor(monkeypatch, fw_config.get_config("mcp_client.servers"))
    assert fake_keystore.get_calls == 0
    assert keystore.is_ref(dlg.controls["mcp_client__token"].getText())
    assert len(workers) == 1

    workers[0]()
    assert dlg.controls["mcp_client__token"].getText() == "tok-async"

    probe_jobs: list[Any] = []
    monkeypatch.setattr(settings_ui, "run_in_background", lambda fn, *_a, **_kw: probe_jobs.append(fn))
    editor.probe_if_changed()
    assert probe_jobs == []


def test_async_token_read_clears_rename_locked_status(
    monkeypatch: pytest.MonkeyPatch,
    fake_keystore: Any,
) -> None:
    from plugin.chatbot import settings_fields
    from plugin.framework import config as fw_config, keystore

    fw_config.save({"mcp_client.servers": {"home": {"url": "http://127.0.0.1:8000/mcp", "token": "tok-async"}}})
    keystore.reset_for_tests(fake_keystore)

    workers: list[Any] = []
    monkeypatch.setattr(settings_fields, "run_in_background", lambda fn, *_a, **_kw: workers.append(fn))
    monkeypatch.setattr(settings_fields.main, "post", lambda fn: fn())
    monkeypatch.setattr(settings_ui.config, "save", lambda _patch: None)

    editor, dlg = _attach_editor(monkeypatch, fw_config.get_config("mcp_client.servers"))
    dlg.controls["mcp_client__name"].setText("home2")
    editor.save()
    assert "Unlock the keyring" in dlg.controls["mcp_client__status"].getText()

    workers[0]()
    assert dlg.controls["mcp_client__token"].getText() == "tok-async"
    assert dlg.controls["mcp_client__status"].getText() == ""
