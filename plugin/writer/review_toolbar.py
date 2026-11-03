"""Show the review fast-travel toolbar only while the document has pending agent changes.

The toolbar is declared statically in Addons.xcu and named/hidden-by-default in
WriterWindowState.xcu. Here we flip its visibility at runtime: shown when
``agent_change_presence(model, stop_at_first=True)`` finds changes, hidden when it drops back to zero. All LayoutManager
calls must run on the UNO main thread -- every caller below already does (the agent edit path is
marshalled via main.call; resolve/navigate handlers and document events fire on the
main thread).
"""

from __future__ import annotations

# pyright: reportUnusedFunction=false

import logging
from typing import Any

import uno
from plugin.framework.uno_context import get_runtime_uid
from plugin.writer import review_view_attachment as rva

log = logging.getLogger("writeragent.review_toolbar")

TOOLBAR_RESOURCE_URL = "private:resource/toolbar/addon_org.extension.writeragent.toolbar"

_DOCK_NEW_ROW_Y = 30000


def _dock_top(lm: Any) -> bool:
    try:
        from com.sun.star.awt import Point

        area = uno.Enum("com.sun.star.ui.DockingArea", "DOCKINGAREA_TOP")
        pos = Point()
        pos.X = 0
        pos.Y = _DOCK_NEW_ROW_Y
        lm.dockWindow(TOOLBAR_RESOURCE_URL, area, pos)
        return True
    except Exception:
        log.debug("review_toolbar: dock-to-top failed", exc_info=True)
        return False


def refresh_review_toolbar(model: Any) -> None:
    if model is None:
        return
    try:
        from plugin.writer.review_scan import agent_change_presence

        lm = rva.layout_manager(model)
        if lm is None:
            return
        count, reliable = agent_change_presence(model, stop_at_first=True)
        if not reliable:
            return
        if count > 0:
            lm.showElement(TOOLBAR_RESOURCE_URL)
            uid = get_runtime_uid(model) or None
            key = rva.attachment_key(model)
            attachment = rva._attachments.get(key)
            docked = attachment.toolbar_docked if attachment is not None else False
            if uid is None or not docked:
                if _dock_top(lm):
                    rva.mark_toolbar_docked(model)
        else:
            lm.hideElement(TOOLBAR_RESOURCE_URL)
    except Exception:
        log.debug("review_toolbar: refresh failed", exc_info=True)


def install_review_toolbar(ctx: Any) -> None:
    rva.install(ctx)
