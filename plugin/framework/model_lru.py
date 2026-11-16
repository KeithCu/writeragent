"""Model combobox sanitization and LRU history management.

Provides helpers for filtering placeholder text from model dropdowns
and managing MRU/LRU lists in configuration.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.config import LRU_MAX_ITEMS, get_config, save
from plugin.framework.i18n import _

_MODEL_COMBO_PLACEHOLDER_MSGIDS = (
    "(Enter API Key to load models)",
    "(Connection failed)",
    "(No image models on this endpoint)",
    "(Default for current endpoint)",
)


def _is_model_combobox_placeholder(val: str) -> bool:
    text = str(val or "").strip()
    for msgid in _MODEL_COMBO_PLACEHOLDER_MSGIDS:
        if text == msgid or text == _(msgid):
            return True
    return False


def sanitize_model_combobox_value(val: str) -> str:
    """Drop Settings combobox placeholder strings; they are not real model ids."""
    cleaned = str(val or "").strip()
    return "" if _is_model_combobox_placeholder(cleaned) else cleaned


def lru_config_key(lru_key: str, endpoint: str) -> str:
    """Config key for an LRU list. Endpoint-scoped lists are ``name@url``."""
    return f"{lru_key}@{endpoint}" if endpoint else lru_key


def next_lru_list(current: Any, val: Any) -> list[str] | None:
    """LRU list after prepending *val*, or None when that would not be a write.

    Blank values are not entries. A value that is already the first item is
    not a write. *current* may be a non-list (a missing key read as something
    else); that still becomes a one-item list.
    """
    val_str = "" if val is None else str(val).strip()
    if not val_str:
        return None
    # LRU entries are model id strings; get_config is JSON-shaped so normalize explicitly.
    lru: list[str] = [str(item) for item in current] if isinstance(current, list) else []
    # Already at the head: a save here would rewrite the file and emit
    # config:changed for a list the UI already shows first.
    if lru and lru[0] == val_str:
        return None
    return ([val_str] + [x for x in dict.fromkeys(lru) if x != val_str])[:LRU_MAX_ITEMS]


def _one_char_short_of(short: str, long: str) -> bool:
    """True when *long* equals *short* with a single character inserted."""
    if len(long) != len(short) + 1:
        return False
    for idx, ch in enumerate(short):
        if long[idx] != ch:
            return short[idx:] == long[idx + 1 :]
    return True


def drop_typed_partials(entries: list[str]) -> list[str]:
    """Entries (newest first) minus any that a newer entry equals with one character inserted.

    Junk entries come from text typed one character at a time into a model
    combo; each intermediate keystroke is one character shorter than the next
    newer entry, so checking against all newer entries collapses the chain.
    """
    out: list[str] = []
    for idx, entry in enumerate(entries):
        if any(_one_char_short_of(entry, newer) for newer in entries[:idx]):
            continue
        out.append(entry)
    return out


def next_model_lru_list(current: Any, val: Any) -> list[str] | None:
    """``next_lru_list`` for model-id lists, also dropping typed partials.

    Returns ``None`` when the stored list would not change. Runs the cleanup
    even when *val* is already the head so an upgraded config is cleaned on
    its first commit.
    """
    lru = [str(x) for x in current] if isinstance(current, list) else []
    cleaned = drop_typed_partials(next_lru_list(lru, val) or lru)
    return None if cleaned == lru else cleaned


def update_lru_history(val: Any, lru_key: str, endpoint: str) -> None:
    """Prepend *val* to an endpoint-scoped LRU list in writeragent.json.

    No write when the value is blank or already the first item. Settings OK
    and the sidebar model sync do not call this once per key: they merge
    ``next_lru_list`` into the same ``save`` as the field change.
    """
    scoped_key = lru_config_key(lru_key, endpoint)
    new_lru = next_lru_list(get_config(scoped_key), val)
    if new_lru is None:
        return
    save({scoped_key: new_lru})
