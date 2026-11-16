# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-place multi-server editor and live probe for Settings > Integrations (``mcp_client.servers``)."""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable

from plugin.chatbot.dialogs import (
    get_checkbox_state,
    get_control_text,
    get_optional,
    set_checkbox_state,
    set_combo_items,
    set_control_text,
)
from plugin.framework import config
from plugin.framework.config import get_config
from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.uno_listeners import BaseActionListener, BaseFocusListener, BaseItemListener
from plugin.framework.worker_pool import run_in_background
from plugin.mcp.client import McpClientError, get_connection
from plugin.mcp.client_helpers import TRUST_FULL, TRUST_UNTRUSTED, normalize_server_trust, sanitize_error, server_enabled

log = logging.getLogger("writeragent.mcp_client.settings_ui")

_CONTROL_KEYS = (
    "server",
    "add_server",
    "remove_server",
    "enabled",
    "trusted",
    "name",
    "url",
    "token_keychain",
    "token",
    "status",
)


class _ServerItemListener(BaseItemListener):
    def __init__(self, editor: McpServersEditor, combo: Any) -> None:
        self._editor: McpServersEditor = editor
        self._combo: Any = combo

    def on_item_state_changed(self, rEvent: Any) -> None:
        idx = getattr(rEvent, "Selected", -1)
        if idx >= 0:
            self._editor.select(str(self._combo.getItem(idx)))


class _AddServerListener(BaseActionListener):
    def __init__(self, editor: McpServersEditor) -> None:
        self._editor: McpServersEditor = editor

    def on_action_performed(self, rEvent: Any) -> None:
        self._editor.add()


class _RemoveServerListener(BaseActionListener):
    def __init__(self, editor: McpServersEditor) -> None:
        self._editor: McpServersEditor = editor

    def on_action_performed(self, rEvent: Any) -> None:
        self._editor.remove()


class _ProbeFocusListener(BaseFocusListener):
    def __init__(self, editor: McpServersEditor) -> None:
        self._editor: McpServersEditor = editor

    def on_focus_lost(self, e: Any) -> None:
        self._editor.probe_if_changed()


class McpServersEditor:
    """Settings > Integrations: edit ``mcp_client.servers`` in place and probe a server when its URL or token changes."""

    def __init__(self, controls: dict[str, Any], servers: dict[str, dict[str, Any]]) -> None:
        self._controls: dict[str, Any] = controls
        self._servers: dict[str, dict[str, Any]] = copy.deepcopy(servers)
        self._current: str | None = next(iter(self._servers), None)
        self._status: dict[str, str] = {}
        self._snapshot: dict[str, tuple[Any, Any, Any]] = {}
        self._pending: dict[str, int] = {}
        self._gen: int = 0
        self._added: set[str] = set()
        self._probed_ok: list[dict[str, Any]] = []
        self._closed: bool = False
        self._cancel: SendCancellation = SendCancellation()

    @classmethod
    def attach(cls, dlg: Any, attach: Callable[[Any, str, Any], None]) -> McpServersEditor | None:
        """Bind the editor to Settings controls; return None when the controls are absent."""
        controls: dict[str, Any] = {}
        for key in _CONTROL_KEYS:
            ctrl = get_optional(dlg, f"mcp_client__{key}")
            if ctrl is None:
                return None
            controls[key] = ctrl
        raw = get_config("mcp_client.servers")
        servers = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
        editor = cls(controls, servers)
        editor.load(next(iter(servers), None))
        attach(controls["server"], "addItemListener", _ServerItemListener(editor, controls["server"]))
        attach(controls["add_server"], "addActionListener", _AddServerListener(editor))
        attach(controls["remove_server"], "addActionListener", _RemoveServerListener(editor))
        for field_key in ("url", "token", "token_keychain"):
            attach(controls[field_key], "addFocusListener", _ProbeFocusListener(editor))
        return editor

    def _unique_name(self, base: str = "server") -> str:
        if base not in self._servers:
            return base
        idx = 2
        while f"{base}-{idx}" in self._servers:
            idx += 1
        return f"{base}-{idx}"

    def load(self, name: str | None) -> None:
        """Populate editor controls for *name* (or clear them when *name* is None)."""
        self._current = name if name in self._servers else None
        entry = self._servers.get(self._current, {}) if self._current is not None else {}
        set_control_text(self._controls["name"], self._current or "")
        set_control_text(self._controls["url"], str(entry.get("url") or ""))
        set_control_text(self._controls["token_keychain"], str(entry.get("token_keychain") or ""))
        set_control_text(self._controls["token"], str(entry.get("token") or ""))
        set_checkbox_state(self._controls["enabled"], server_enabled(entry) if self._current is not None else True)
        set_checkbox_state(self._controls["trusted"], normalize_server_trust(entry.get("trust")) == TRUST_FULL)
        set_control_text(self._controls["status"], self._status.get(self._current or "", ""))
        set_combo_items(self._controls["server"], list(self._servers), selected=self._current or "")

    def commit(self) -> None:
        """Write current field values back into ``self._servers``, creating or renaming as needed."""
        raw_name = get_control_text(self._controls["name"]).strip()
        url = get_control_text(self._controls["url"]).strip()
        token = get_control_text(self._controls["token"]).strip()
        token_keychain = get_control_text(self._controls["token_keychain"]).strip()
        enabled = bool(get_checkbox_state(self._controls["enabled"]))
        trusted = bool(get_checkbox_state(self._controls["trusted"]))

        if self._current is None:
            if not raw_name and not url:
                return
            created = self._unique_name(raw_name or "server")
            self._servers[created] = {}
            self._added.add(created)
            self._current = created

        old = self._current
        entry = dict(self._servers.get(old, {}))
        entry["url"] = url
        entry["enabled"] = enabled
        entry["trust"] = TRUST_FULL if trusted else TRUST_UNTRUSTED
        if token:
            entry["token"] = token
        else:
            entry.pop("token", None)
        if token_keychain:
            entry["token_keychain"] = token_keychain
        else:
            entry.pop("token_keychain", None)

        if raw_name and raw_name != old:
            if raw_name in self._servers:
                self._servers[old] = entry
                msg = _("A server named {name} already exists.").format(name=raw_name)
                self._status[old] = msg
                set_control_text(self._controls["status"], msg)
                return
            self._servers.pop(old, None)
            self._servers[raw_name] = entry
            if old in self._status:
                self._status[raw_name] = self._status.pop(old)
            if old in self._snapshot:
                self._snapshot[raw_name] = self._snapshot.pop(old)
            if old in self._added:
                self._added.discard(old)
                self._added.add(raw_name)
            self._pending.pop(old, None)
            self._current = raw_name
            set_combo_items(self._controls["server"], list(self._servers), selected=raw_name)
            return

        self._servers[old] = entry

    def select(self, name: str) -> None:
        """Switch the active server in the editor after committing current edits."""
        if name == self._current or name not in self._servers:
            return
        self.commit()
        self.load(name)

    def add(self) -> None:
        """Create a new enabled server entry and select it."""
        self.commit()
        name = self._unique_name("server")
        self._servers[name] = {"enabled": True}
        self._added.add(name)
        self.load(name)

    def remove(self) -> None:
        """Delete the currently selected server entry and load the next available one."""
        if self._current is None:
            return
        old = self._current
        self._servers.pop(old, None)
        self._status.pop(old, None)
        self._snapshot.pop(old, None)
        self._pending.pop(old, None)
        self._added.discard(old)
        self.load(next(iter(self._servers), None))

    def probe_if_changed(self) -> None:
        """Probe the current server off the main thread when its URL or token triple changed."""
        if self._closed:
            return
        self.commit()
        if self._current is None:
            return
        name = self._current
        entry = {**self._servers[name], "name": name}
        snap = (entry.get("url"), entry.get("token"), entry.get("token_keychain"))
        if not entry.get("url") or snap == self._snapshot.get(name):
            return
        self._snapshot[name] = snap
        self._gen += 1
        gen = self._gen
        self._pending[name] = gen
        checking = _("Checking…")
        self._status[name] = checking
        set_control_text(self._controls["status"], checking)
        run_in_background(self._probe, entry, name, gen, name="mcp-settings-probe", dedicated=True)

    def _probe(self, entry: dict[str, Any], name: str, gen: int) -> None:
        ok = False
        try:
            count = len(get_connection(entry).list_tools(cancel=self._cancel))
            text = _("Connected: {count} tools").format(count=count)
            ok = True
        except McpClientError as e:
            text = _("Failed: {error}").format(error=str(e))
        except Exception as e:
            log.exception("MCP server probe failed for %s", name)
            text = _("Failed: {error}").format(error=sanitize_error(str(e)))
        main.post(lambda: self._apply(name, gen, entry, text, ok))

    def _apply(self, name: str, gen: int, entry: dict[str, Any], text: str, ok: bool) -> None:
        if self._closed or self._pending.get(name) != gen:
            return
        self._pending.pop(name, None)
        self._status[name] = text
        if ok:
            self._probed_ok.append(entry)
        if self._current == name:
            set_control_text(self._controls["status"], text)

    def save(self) -> list[dict[str, Any]]:
        """Commit edits, persist ``mcp_client.servers`` if changed, and return unchanged probed entries."""
        self.commit()
        servers = {
            k: v
            for k, v in self._servers.items()
            if not (k in self._added and not str(v.get("url") or "").strip())
        }
        if servers != get_config("mcp_client.servers"):
            config.save({"mcp_client.servers": servers})
        return [e for e in self._probed_ok if {**servers.get(e["name"], {}), "name": e["name"]} == e]

    def close(self) -> None:
        """Mark the editor closed and cancel any in-flight probe requests."""
        self._closed = True
        self._cancel.cancel()
