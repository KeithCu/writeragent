# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer right-click review: accept/reject the agent's tracked changes, as whole units.

Adds two entries to the Writer text context menu (only while the document holds pending AGENT
changes -- wa-review session tokens): accept/reject the agent change under the cursor as one
unit (a replace's strikethrough + underline resolve together, no Manage dialog). The user's own
tracked changes are never touched. Bulk accept/reject ALL agent changes lives on the review
toolbar (#2), not in this menu. The entries dispatch
``org.extension.writeragent:writer.accept_change`` / ``writer.reject_change``, handled in ``main.py``.

PyUNO pitfalls this file works around (each silently killed the menu when done wrong):
- ``queryInterface(InterfaceClass)`` cannot convert an imported interface class to a UNO type;
  call the interface's methods directly on the proxy object instead.
- An exception escaping ``notifyContextMenuExecute`` makes the core permanently UNREGISTER the
  interceptor (``catch (...) { removeInterface }`` in SfxViewShell::TryContextMenuInterception)
  with no diagnostics -- so the entire body, imports included, lives inside try/except.
- ``registerContextMenuInterceptor`` on a controller that is not (yet/anymore) attached to a
  view shell succeeds and does nothing. Registration therefore prefers ``Event.ViewController``
  from OnViewCreated (broadcast only after the view shell is attached) and re-registers per new
  controller; interceptors do not survive a view recreation (reload, new window).
"""

from __future__ import annotations

import logging
from typing import Any

import unohelper

from plugin.framework.constants import EXTENSION_ID_WRITERAGENT

log = logging.getLogger(__name__)

_ACCEPT_URL = f"{EXTENSION_ID_WRITERAGENT}:writer.accept_change"
_REJECT_URL = f"{EXTENSION_ID_WRITERAGENT}:writer.reject_change"

_interceptor_cls: Any = None


def _build_menu(model: Any, event: Any, ignored: Any, continue_modified: Any) -> Any:
    try:
        from com.sun.star.ui import ActionTriggerSeparatorType

        from plugin.framework.i18n import _
        from plugin.writer.review_scan import agent_change_presence

        count, reliable = agent_change_presence(model, stop_at_first=True)
        has = count > 0 or not reliable
        log.debug("change_context_menu: notifyContextMenuExecute (Writer), has_agent_changes=%s", has)
        if not has:
            return ignored
        container = event.ActionTriggerContainer
        if not hasattr(container, "createInstance"):
            return ignored

        separator = container.createInstance("com.sun.star.ui.ActionTriggerSeparator")
        separator.setPropertyValue("SeparatorType", ActionTriggerSeparatorType.LINE)
        accept = container.createInstance("com.sun.star.ui.ActionTrigger")
        accept.setPropertyValue("Text", _("Accept whole change (agent)"))
        accept.setPropertyValue("CommandURL", _ACCEPT_URL)
        reject = container.createInstance("com.sun.star.ui.ActionTrigger")
        reject.setPropertyValue("Text", _("Reject whole change (agent)"))
        reject.setPropertyValue("CommandURL", _REJECT_URL)

        insert_at = container.getCount()
        container.insertByIndex(insert_at, separator)
        container.insertByIndex(insert_at + 1, accept)
        container.insertByIndex(insert_at + 2, reject)
        log.debug("change_context_menu: added review entries at index %d", insert_at)
        return continue_modified
    except Exception:
        log.exception("change_context_menu: building menu failed")
        return ignored


def make_interceptor(model: Any) -> Any:
    global _interceptor_cls
    if _interceptor_cls is None:
        from com.sun.star.ui import XContextMenuInterceptor
        from com.sun.star.ui.ContextMenuInterceptorAction import CONTINUE_MODIFIED, IGNORED

        class _ChangeContextMenuInterceptor(unohelper.Base, XContextMenuInterceptor):  # type: ignore[misc, valid-type]
            model: Any

            def __init__(self, model: Any) -> None:
                super().__init__()
                self.model = model

            def notifyContextMenuExecute(self, aEvent: Any) -> Any:  # noqa: N802, N803 -- UNO API
                return _build_menu(self.model, aEvent, IGNORED, CONTINUE_MODIFIED)

        _interceptor_cls = _ChangeContextMenuInterceptor
    return _interceptor_cls(model)
