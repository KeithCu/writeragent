# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Weekly background check against published update.xml (GitHub).

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

log = logging.getLogger(__name__)

UPDATE_XML_URL = "https://raw.githubusercontent.com/KeithCu/writeragent/refs/heads/master/update.xml"
CONFIG_KEY_EXTENSION_UPDATE_CHECK_EPOCH = "extension_update_check_epoch"
WEEK_SECONDS = 7 * 24 * 3600
EXPECTED_EXTENSION_ID = "org.extension.writeragent"
_FETCH_TIMEOUT = 15


def version_tuple(s: str) -> tuple[int, ...] | None:
    """Parse a dotted numeric version into a tuple for ordering. Returns None if invalid."""
    s = (s or "").strip()
    if not s:
        return None
    parts: list[int] = []
    for part in s.split("."):
        if not part.isdigit():
            return None
        parts.append(int(part))
    return tuple(parts)


def parse_update_xml(data: bytes) -> tuple[str | None, str | None]:
    """Return (identifier, version) from update.xml bytes, or (None, None) on parse failure."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        log.debug("extension update check: XML parse error: %s", e)
        return None, None
    ident: str | None = None
    ver: str | None = None
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "identifier":
            ident = el.get("value")
        elif tag == "version":
            ver = el.get("value")
    return ident, ver


def remote_is_newer(remote: str, local: str) -> bool:
    rt = version_tuple(remote)
    lt = version_tuple(local)
    if rt is None or lt is None:
        return False
    return rt > lt


def run_extension_update_check(ctx: Any) -> None:
    """Background worker: fetch update.xml, compare versions, optionally notify. Call after init_logging."""
    from plugin.framework.config import set_config
    from plugin.framework.dialogs import msgbox
    from plugin.framework.queue_executor import QueueExecutor
    from plugin.modules.http.requests import sync_request
    from plugin.version import EXTENSION_VERSION

    attempted = False
    try:
        log.info(
            "extension update check: worker started (local EXTENSION_VERSION=%r, url=%s)",
            EXTENSION_VERSION,
            UPDATE_XML_URL,
        )
        now = time.time()
        from plugin.framework.config import get_config_int

        raw_last = get_config_int(ctx, CONFIG_KEY_EXTENSION_UPDATE_CHECK_EPOCH)
        if raw_last is not None and raw_last != "":
            try:
                last_ts = float(str(raw_last))
                age = now - last_ts
                if age < WEEK_SECONDS:
                    log.info(
                        "extension update check: skipped (last attempt %.1f h ago; next fetch in %.1f h). Remove key %r from writeragent.json to force a run sooner.",
                        age / 3600.0,
                        (WEEK_SECONDS - age) / 3600.0,
                        CONFIG_KEY_EXTENSION_UPDATE_CHECK_EPOCH,
                    )
                    return
            except (TypeError, ValueError):
                log.info(
                    "extension update check: ignoring invalid %r value %r",
                    CONFIG_KEY_EXTENSION_UPDATE_CHECK_EPOCH,
                    raw_last,
                )

        attempted = True
        log.info("extension update check: fetching update.xml …")
        raw = sync_request(UPDATE_XML_URL, parse_json=False, timeout=_FETCH_TIMEOUT)
        if not isinstance(raw, bytes):
            log.warning("extension update check: unexpected response type %s", type(raw).__name__)
            return
        log.info("extension update check: received %s bytes", len(raw))
        ident, remote_ver = parse_update_xml(raw)
        log.info(
            "extension update check: parsed identifier=%r remote_version=%r",
            ident,
            remote_ver,
        )
        if ident != EXPECTED_EXTENSION_ID:
            log.info(
                "extension update check: identifier mismatch (got %r, expected %r), not notifying",
                ident,
                EXPECTED_EXTENSION_ID,
            )
            return
        if not remote_ver:
            log.info("extension update check: no version in XML, not notifying")
            return
        rt = version_tuple(remote_ver)
        lt = version_tuple(EXTENSION_VERSION)
        log.info(
            "extension update check: comparing remote_tuple=%s local_tuple=%s (local %r)",
            rt,
            lt,
            EXTENSION_VERSION,
        )
        if not remote_is_newer(remote_ver, EXTENSION_VERSION):
            log.info(
                "extension update check: not notifying (remote %s is not newer than local %s)",
                remote_ver,
                EXTENSION_VERSION,
            )
            return

        from plugin.framework.i18n import _

        def _show() -> None:
            title = _("Update available")
            message = _("A newer WriterAgent (%s) is available. Use Tools → Extension Manager to check for updates and install the latest extension.") % (remote_ver,)
            msgbox(ctx, title, message)

        log.info(
            "extension update check: posting update dialog (remote %s > local %s)",
            remote_ver,
            EXTENSION_VERSION,
        )
        QueueExecutor().post(_show)
    except Exception as e:
        log.warning(
            "extension update check failed: %s",
            e,
            exc_info=True,
        )
    finally:
        if attempted:
            set_config(ctx, CONFIG_KEY_EXTENSION_UPDATE_CHECK_EPOCH, time.time())
            log.info(
                "extension update check: recorded %s in config (attempt finished)",
                CONFIG_KEY_EXTENSION_UPDATE_CHECK_EPOCH,
            )
