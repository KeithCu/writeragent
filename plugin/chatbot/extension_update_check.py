# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
"""Extension version check vs published ``update.xml`` (weekly GitHub fetch; not chat logic).

Shared by WriterAgent, LibrePy, and LibreHarper. Scheduling is once per process per
product (sidebar wire / StartupJob / HarperProofreader); this module owns fetch,
compare, notify, and the schedule helper.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET  # nosemgrep: use-defused-xml  # trusted small update.xml from project repo (Bandit B314)
from dataclasses import dataclass
from typing import Any

from plugin.framework.constants import (
    EXTENSION_ID_LIBREHARPER,
    EXTENSION_ID_LIBREPY,
    EXTENSION_ID_WRITERAGENT,
)

log = logging.getLogger(__name__)

WEEK_SECONDS = 7 * 24 * 3600
_FETCH_TIMEOUT = 15
_GITHUB_RAW = "https://raw.githubusercontent.com/KeithCu/writeragent/refs/heads/master"

_scheduled_extension_ids: set[str] = set()
_schedule_lock = threading.Lock()


@dataclass(frozen=True)
class UpdateCheckProfile:
    extension_id: str
    display_name: str
    update_xml_url: str
    config_key_epoch: str


UPDATE_CHECK_PROFILES: dict[str, UpdateCheckProfile] = {
    EXTENSION_ID_WRITERAGENT: UpdateCheckProfile(
        extension_id=EXTENSION_ID_WRITERAGENT,
        display_name="WriterAgent",
        update_xml_url=f"{_GITHUB_RAW}/update.xml",
        config_key_epoch="extension_update_check_epoch",
    ),
    EXTENSION_ID_LIBREPY: UpdateCheckProfile(
        extension_id=EXTENSION_ID_LIBREPY,
        display_name="LibrePy",
        update_xml_url=f"{_GITHUB_RAW}/update-librepy.xml",
        config_key_epoch="librepy_update_check_epoch",
    ),
    EXTENSION_ID_LIBREHARPER: UpdateCheckProfile(
        extension_id=EXTENSION_ID_LIBREHARPER,
        display_name="LibreHarper",
        update_xml_url=f"{_GITHUB_RAW}/update-libreharper.xml",
        config_key_epoch="libreharper_update_check_epoch",
    ),
}


def get_update_check_profile(extension_id: str | None) -> UpdateCheckProfile | None:
    """Return the profile for ``extension_id``, or None if unknown."""
    if not extension_id:
        return None
    return UPDATE_CHECK_PROFILES.get(extension_id)


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


def schedule_extension_update_check_once(ctx: Any, extension_id: str | None = None) -> None:
    """Run weekly update check at most once per process per product, after init_logging."""
    from plugin.framework.uno_context import resolve_package_extension_id

    eid = extension_id or resolve_package_extension_id(ctx)
    profile = get_update_check_profile(eid)
    if profile is None:
        log.info("extension update check: no profile for %r, not scheduling", eid)
        return
    with _schedule_lock:
        if eid in _scheduled_extension_ids:
            log.info("extension update check: already queued this process for %s, skipping", eid)
            return
        _scheduled_extension_ids.add(eid)
    from plugin.framework.worker_pool import run_in_background

    log.info("extension update check: scheduling background worker for %s", eid)
    run_in_background(run_extension_update_check, ctx, name=f"extension_update_check_{profile.display_name}", extension_id=eid, dedicated=True)


def reset_extension_update_check_schedule_for_tests() -> None:
    """Clear once-per-process schedule set (unit tests only)."""
    with _schedule_lock:
        _scheduled_extension_ids.clear()


from plugin.framework.thread_guard import background


@background
def run_extension_update_check(ctx: Any, extension_id: str | None = None) -> None:
    """Background worker: fetch update.xml, compare versions, optionally notify. Call after init_logging."""
    from plugin.framework.config import set_config
    from plugin.chatbot.dialogs import msgbox
    from plugin.framework.client.requests import sync_request
    from plugin.framework.uno_context import resolve_package_extension_id
    from plugin.version import EXTENSION_VERSION

    eid = extension_id or resolve_package_extension_id(ctx)
    profile = get_update_check_profile(eid)
    if profile is None:
        log.info("extension update check: no profile for %r, not running", eid)
        return

    attempted = False
    try:
        log.info(
            "extension update check: worker started (product=%s local EXTENSION_VERSION=%r, url=%s)",
            profile.display_name,
            EXTENSION_VERSION,
            profile.update_xml_url,
        )
        now = time.time()
        from plugin.framework.config import get_config_int

        raw_last = get_config_int(profile.config_key_epoch)
        if raw_last is not None and raw_last != "":
            try:
                last_ts = float(str(raw_last))
                age = now - last_ts
                if age < WEEK_SECONDS:
                    log.info(
                        "extension update check: skipped %s (last attempt %.1f h ago; next fetch in %.1f h). Remove key %r from writeragent.json to force a run sooner.",
                        profile.display_name,
                        age / 3600.0,
                        (WEEK_SECONDS - age) / 3600.0,
                        profile.config_key_epoch,
                    )
                    return
            except (TypeError, ValueError):
                log.info("extension update check: ignoring invalid %r value %r", profile.config_key_epoch, raw_last)

        attempted = True
        log.info("extension update check: fetching %s …", profile.update_xml_url)
        raw = sync_request(profile.update_xml_url, parse_json=False, timeout=_FETCH_TIMEOUT)
        if not isinstance(raw, bytes):
            log.warning("extension update check: unexpected response type %s", type(raw).__name__)
            return
        log.info("extension update check: received %s bytes", len(raw))
        ident, remote_ver = parse_update_xml(raw)
        log.info("extension update check: parsed identifier=%r remote_version=%r", ident, remote_ver)
        if ident != profile.extension_id:
            log.info(
                "extension update check: identifier mismatch (got %r, expected %r), not notifying",
                ident,
                profile.extension_id,
            )
            return
        if not remote_ver:
            log.info("extension update check: no version in XML, not notifying")
            return
        rt = version_tuple(remote_ver)
        lt = version_tuple(EXTENSION_VERSION)
        log.info("extension update check: comparing remote_tuple=%s local_tuple=%s (local %r)", rt, lt, EXTENSION_VERSION)
        if not remote_is_newer(remote_ver, EXTENSION_VERSION):
            log.info(
                "extension update check: not notifying (remote %s is not newer than local %s)",
                remote_ver,
                EXTENSION_VERSION,
            )
            return

        from plugin.framework.i18n import _

        product_name = profile.display_name

        def _show() -> None:
            title = _("Update available")
            message = _(
                "A newer %(name)s (%(version)s) is available. Use Tools → Extension Manager to check for updates and install the latest extension."
            ) % {"name": product_name, "version": remote_ver}
            msgbox(ctx, title, message)

        log.info(
            "extension update check: posting update dialog for %s (remote %s > local %s)",
            product_name,
            remote_ver,
            EXTENSION_VERSION,
        )
        # A throwaway QueueExecutor is not drained by pump_ui_idle. If
        # AsyncCallback is not ready, post() parks the dialog on that
        # executor and the object is collected. default_executor is pumped.
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(_show)
    except Exception as e:
        log.warning("extension update check failed (%s): %s", profile.display_name, e, exc_info=True)
    finally:
        if attempted:
            try:
                set_config(profile.config_key_epoch, time.time())
                log.info("extension update check: recorded %s in config (attempt finished)", profile.config_key_epoch)
            except Exception as e:
                log.warning("extension update check: failed to record %s in config: %s", profile.config_key_epoch, e)
