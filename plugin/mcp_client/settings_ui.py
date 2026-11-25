# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-place multi-server editor and live probe for Settings > Integrations (``mcp_client.servers``).

All editor state is read and mutated only on the main thread (UI listeners,
``save()``, ``close()``, and callbacks posted via ``main.post``). Background
probe workers receive a snapshot of one server entry and never touch editor
state directly; stale replies are discarded by generation counter and ``_closed``.
"""

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
from plugin.chatbot.settings_fields import read_secret_in_background
from plugin.framework import config, keystore
from plugin.framework.config import entry_secret_field_text, get_config, resolve_entry_secret
from plugin.framework.i18n import N_, _
from plugin.framework.queue_executor import main
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.uno_listeners import BaseActionListener, BaseFocusListener, BaseItemListener
from plugin.framework.worker_pool import run_in_background
from plugin.mcp_client.client import McpClientError, McpServerConnection, SERVERS_KEY, probe_server
from plugin.mcp_client.client_helpers import TRUST_FULL, TRUST_UNTRUSTED, sanitize_error, server_enabled, server_trusted

log = logging.getLogger("writeragent.mcp_client.settings_ui")

_RENAME_LOCKED_MSG = N_("Unlock the keyring or clear the Token field before renaming this server.")

_CONTROL_KEYS = (
    "server",
    "add_server",
    "remove_server",
    "enabled",
    "trusted",
    "name",
    "url",
    "token",
    "status",
)


class _ServerItemListener(BaseItemListener):
    def __init__(self, editor: McpServersEditor, combo: Any) -> None:
        self._editor: McpServersEditor = editor
        self._combo: Any = combo

    def on_item_state_changed(self, rEvent: Any) -> None:
        if rEvent.Selected >= 0:
            self._editor.select(get_control_text(self._combo))


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

    def __init__(self, controls: dict[str, Any], servers: dict[str, Any]) -> None:
        self._controls: dict[str, Any] = controls
        self._servers: dict[str, Any] = copy.deepcopy(servers)
        self._current: str | None = next(iter(self._dict_names()), None)
        self._snapshot: dict[str, tuple[str, str]] = {}
        self._status: dict[str, str] = {}
        self._inflight: dict[str, tuple[int, SendCancellation]] = {}
        self._probed: dict[str, tuple[dict[str, Any], McpServerConnection]] = {}
        self._gen: int = 0
        self._closed: bool = False

    def _dict_names(self) -> list[str]:
        return [k for k, v in self._servers.items() if isinstance(v, dict)]

    @classmethod
    def attach(cls, dlg: Any, attach: Callable[[Any, str, Any], None]) -> McpServersEditor | None:
        """Bind the editor to Settings controls; return None when the controls are absent."""
        controls: dict[str, Any] = {}
        for key in _CONTROL_KEYS:
            ctrl = get_optional(dlg, f"mcp_client__{key}")
            if ctrl is None:
                return None
            controls[key] = ctrl
        raw = get_config(SERVERS_KEY)
        servers = raw if isinstance(raw, dict) else {}
        editor = cls(controls, servers)
        editor._load(next(iter(editor._dict_names()), None))
        attach(controls["server"], "Item", _ServerItemListener(editor, controls["server"]))
        attach(controls["add_server"], "Action", _AddServerListener(editor))
        attach(controls["remove_server"], "Action", _RemoveServerListener(editor))
        for field_key in ("url", "token"):
            attach(controls[field_key], "Focus", _ProbeFocusListener(editor))
        return editor

    def _unique_name(self) -> str:
        base = "server"
        if base not in self._servers:
            return base
        idx = 2
        while f"{base}-{idx}" in self._servers:
            idx += 1
        return f"{base}-{idx}"

    def _cancel_inflight(self, name: str) -> None:
        entry = self._inflight.pop(name, None)
        if entry is not None:
            entry[1].cancel()

    def _on_token_read(self, name: str, raw_token: Any) -> None:
        token_text = entry_secret_field_text(SERVERS_KEY, name, "token", raw_token)
        if keystore.is_ref(token_text):
            if not self._status.get(name) and not get_control_text(self._controls["status"]).strip():
                set_control_text(
                    self._controls["status"],
                    _("The token is in the keyring but could not be read. Unlock the keyring to see it, or clear the Token field to remove it."),
                )
            return
        set_control_text(self._controls["token"], token_text)
        self._snapshot[name] = (self._snapshot[name][0], token_text.strip())
        if self._status.get(name) == _(_RENAME_LOCKED_MSG):
            self._status.pop(name, None)
            set_control_text(self._controls["status"], "")

    def _start_token_read(self, name: str, raw_token: Any, token_ref: str) -> None:
        read_secret_in_background(
            self._controls["token"],
            token_ref,
            read=lambda: resolve_entry_secret(SERVERS_KEY, name, "token", raw_token),
            on_done=lambda: self._on_token_read(name, raw_token),
            is_closed=lambda: self._closed or self._current != name,
        )

    def _load(self, name: str | None) -> None:
        """Populate editor controls for *name* (or a blank form when *name* is None)."""
        raw_token: Any = None
        token_text = ""
        if name is not None:
            self._current = name
            entry = self._servers[name]
            raw_token = entry.get("token")
            token_text = entry_secret_field_text(SERVERS_KEY, name, "token", raw_token)
            url_text = str(entry.get("url") or "")
            enabled = server_enabled(entry)
            trusted = server_trusted(entry)
            status_text = self._status.get(name, "")
        else:
            self._current = None
            url_text = ""
            enabled = True
            trusted = False
            status_text = ""

        set_control_text(self._controls["name"], self._current or "")
        set_control_text(self._controls["url"], url_text)
        set_control_text(self._controls["token"], token_text)
        set_checkbox_state(self._controls["enabled"], enabled)
        set_checkbox_state(self._controls["trusted"], trusted)
        set_control_text(self._controls["status"], status_text)
        set_combo_items(self._controls["server"], self._dict_names(), selected=self._current or "")
        # set_combo_items keeps the previous text when the list is non-empty and selected is "".
        set_control_text(self._controls["server"], self._current or "")
        if self._current is not None:
            self._snapshot[self._current] = (
                get_control_text(self._controls["url"]).strip(),
                get_control_text(self._controls["token"]).strip(),
            )
            if keystore.is_ref(token_text):
                self._start_token_read(self._current, raw_token, token_text)

    def _commit(self) -> None:
        """Write current field values back into ``self._servers``, creating or renaming as needed."""
        raw_name = get_control_text(self._controls["name"]).strip()
        url = get_control_text(self._controls["url"]).strip()
        token = get_control_text(self._controls["token"]).strip()
        enabled = bool(get_checkbox_state(self._controls["enabled"]))
        trusted = bool(get_checkbox_state(self._controls["trusted"]))

        if self._current is None:
            if not raw_name and not url:
                return
            if raw_name and raw_name in self._servers:
                msg = _("A server named {name} already exists.").format(name=raw_name)
                set_control_text(self._controls["status"], msg)
                return
            created = raw_name or self._unique_name()
            if not raw_name:
                set_control_text(self._controls["name"], created)
            self._servers[created] = {}
            self._current = created
            set_combo_items(self._controls["server"], self._dict_names(), selected=created)

        old = self._current
        entry = dict(self._servers[old])
        if url:
            entry["url"] = url
        else:
            entry.pop("url", None)
        # Write enabled/trust only when they differ from the effective value so hand-written entries stay byte-identical on OK.
        if enabled != server_enabled(entry):
            entry["enabled"] = enabled
        current_trusted = server_trusted(entry)
        if trusted != current_trusted:
            entry["trust"] = TRUST_FULL if trusted else TRUST_UNTRUSTED
        if token:
            entry["token"] = token
        else:
            entry.pop("token", None)

        if raw_name and raw_name != old:
            if raw_name in self._servers:
                self._servers[old] = entry
                msg = _("A server named {name} already exists.").format(name=raw_name)
                self._status[old] = msg
                set_control_text(self._controls["status"], msg)
                return
            if keystore.is_ref(entry.get("token")):
                self._servers[old] = entry
                msg = _(_RENAME_LOCKED_MSG)
                self._status[old] = msg
                set_control_text(self._controls["status"], msg)
                return
            del self._servers[old]
            self._servers[raw_name] = entry
            if old in self._status:
                self._status[raw_name] = self._status.pop(old)
            self._cancel_inflight(old)
            self._probed.pop(old, None)
            self._snapshot.pop(old, None)
            self._current = raw_name
            set_combo_items(self._controls["server"], self._dict_names(), selected=raw_name)
            return

        self._servers[old] = entry

    def select(self, name: str) -> None:
        """Switch the active server in the editor after committing current edits."""
        if name == self._current or not isinstance(self._servers.get(name), dict):
            return
        self._commit()
        self._load(name)

    def add(self) -> None:
        """Commit current edits and show a blank form for a new server."""
        self._commit()
        self._load(None)

    def remove(self) -> None:
        """Delete the currently selected server entry and load the next available one."""
        if self._current is None:
            return
        old = self._current
        self._servers.pop(old, None)
        self._cancel_inflight(old)
        self._probed.pop(old, None)
        self._status.pop(old, None)
        self._snapshot.pop(old, None)
        self._load(next(iter(self._dict_names()), None))

    def probe_if_changed(self) -> None:
        """Probe the current server off the main thread when its URL or token changed."""
        if self._closed:
            return
        self._commit()
        if self._current is None:
            return
        name = self._current
        url_text = get_control_text(self._controls["url"]).strip()
        token_text = get_control_text(self._controls["token"]).strip()
        if not url_text:
            self._status.pop(name, None)
            set_control_text(self._controls["status"], "")
            return
        snap = (url_text, token_text)
        if snap == self._snapshot.get(name):
            return
        self._snapshot[name] = snap
        self._cancel_inflight(name)
        self._gen += 1
        gen = self._gen
        cancel = SendCancellation()
        self._inflight[name] = (gen, cancel)
        self._probed.pop(name, None)
        checking = _("Checking…")
        self._status[name] = checking
        set_control_text(self._controls["status"], checking)
        entry = {**self._servers[name], "name": name}
        run_in_background(self._probe_worker, entry, name, gen, cancel, name="mcp-settings-probe", dedicated=True)

    def _probe_worker(self, entry: dict[str, Any], name: str, gen: int, cancel: SendCancellation) -> None:
        conn: McpServerConnection | None = None
        text = ""
        try:
            conn = probe_server(entry, cancel)
            text = _("Connected: {count} tools").format(count=len(conn.list_tools()))
        except McpClientError as e:
            text = _("Failed: {error}").format(error=e)
        except Exception as e:
            log.exception("MCP server probe failed for %s", name)
            text = _("Failed: {error}").format(error=sanitize_error(str(e)))
        finally:
            main.post(lambda: self._apply_probe(name, gen, entry, conn, text))

    def _apply_probe(
        self,
        name: str,
        gen: int,
        entry: dict[str, Any],
        conn: McpServerConnection | None,
        text: str,
    ) -> None:
        if self._closed:
            return
        inflight = self._inflight.get(name)
        if inflight is None or inflight[0] != gen:
            return
        self._inflight.pop(name, None)
        self._status[name] = text
        if conn is not None:
            self._probed[name] = (entry, conn)
        if self._current == name:
            set_control_text(self._controls["status"], text)

    def save(self) -> list[McpServerConnection]:
        """Commit edits, persist ``mcp_client.servers`` if changed, and return unchanged probed connections."""
        self._commit()
        if self._servers != get_config(SERVERS_KEY):
            config.save({SERVERS_KEY: self._servers})
        return [
            conn
            for name, (entry, conn) in self._probed.items()
            if isinstance(self._servers.get(name), dict) and {**self._servers[name], "name": name} == entry
        ]

    def close(self) -> None:
        """Mark the editor closed and cancel any in-flight probe requests."""
        self._closed = True
        for _gen, cancel in list(self._inflight.values()):
            cancel.cancel()
        self._inflight.clear()
