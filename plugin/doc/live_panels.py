# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Process-wide registry of weakly referenced sidebar panels keyed by RuntimeUID.

A1 peer messaging looks up the target ``SendButtonListener`` by the peer
document's uid. This is a production registry (not the debug ``WeakSet`` in
``panel_factory``, which is gated / stripped in release).

``panel_factory`` registers after ``_wire_buttons``. The peer tool reads this
module only — it must not import ``panel_factory`` or ``panel`` (cycle:
``CommonModule`` → tool → ``panel`` → ``get_tools()``).
"""

from __future__ import annotations

from typing import Any
from weakref import ref

# One uid can have several windows (File > New Window); newest live panel wins.
_PANELS: dict[str, list[Any]] = {}  # uid -> weakrefs, oldest first


def _live_refs(uid: str) -> list[Any]:
    return _PANELS.get(uid) or []


def register_live_panel(uid: str, panel: Any) -> None:
    """Remember *panel* for *uid*. Empty uid is skipped (cannot address)."""
    if not uid or panel is None:
        return
    refs = [r for r in _live_refs(uid) if r() is not None and r() is not panel]
    refs.append(ref(panel))
    _PANELS[uid] = refs


def unregister_live_panel(uid: str, panel: Any) -> None:
    """Drop *panel* for *uid*; other windows on the same model stay registered.

    Removes the uid key only when no live panel for it remains.
    """
    if not uid:
        return
    refs = [r for r in _live_refs(uid) if r() is not None and r() is not panel]
    if refs:
        _PANELS[uid] = refs
    else:
        _PANELS.pop(uid, None)


def get_live_panel(uid: str) -> Any | None:
    """Return the most recently registered live panel for *uid*, or None."""
    if not uid:
        return None
    for panel_ref in reversed(_live_refs(uid)):
        panel = panel_ref()
        if panel is not None:
            return panel
    return None


def iter_live_panel_uids() -> list[str]:
    """Snapshot of uids that still have a live panel (tests / catalog)."""
    return [uid for uid, refs in list(_PANELS.items()) if any(r() is not None for r in refs)]


def reset_live_panels() -> None:
    """Test hook: clear the weak map."""
    _PANELS.clear()
