# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Process-wide weak map of open sidebar panels keyed by RuntimeUID.

A1 peer messaging looks up the target ``SendButtonListener`` by the peer
document's uid. This is a production registry (not the debug ``WeakSet`` in
``panel_factory``, which is gated / stripped in release).

``panel_factory`` registers after ``_wire_buttons``. The peer tool reads this
module only — it must not import ``panel_factory`` or ``panel`` (cycle:
``CommonModule`` → tool → ``panel`` → ``get_tools()``).
"""

from __future__ import annotations

from typing import Any
from weakref import WeakValueDictionary

# Last-write-wins when two windows share one model (one RuntimeUID).
_PANELS: WeakValueDictionary[str, Any] = WeakValueDictionary()


def register_live_panel(uid: str, panel: Any) -> None:
    """Remember *panel* for *uid*. Empty uid is skipped (cannot address)."""
    if not uid or panel is None:
        return
    _PANELS[uid] = panel


def unregister_live_panel(uid: str, panel: Any = None) -> None:
    """Drop the map slot for *uid* when it still points at *panel*.

    Closing one window of a shared model must not pop the uid after
    another window has registered. Peer send then misses the
    sidebar that is still open. *panel* None keeps the
    unconditional pop for callers that are tearing down the only
    known owner.
    """
    if not uid:
        return
    if panel is not None and _PANELS.get(uid) is not panel:
        return
    _PANELS.pop(uid, None)


def get_live_panel(uid: str) -> Any | None:
    """Return the live panel handle for *uid*, or None."""
    if not uid:
        return None
    return _PANELS.get(uid)


def iter_live_panel_uids() -> list[str]:
    """Snapshot of registered uids (tests / catalog)."""
    return list(_PANELS.keys())


def reset_live_panels() -> None:
    """Test hook: clear the weak map."""
    _PANELS.clear()
