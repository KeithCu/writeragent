"""Model combobox sanitization and LRU history management.

Provides helpers for filtering placeholder text from model dropdowns
and managing MRU/LRU lists in configuration.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.config import LRU_MAX_ITEMS, get_config, set_config
from plugin.framework.i18n import _

__all__ = [
    "_MODEL_COMBO_PLACEHOLDER_MSGIDS",
    "_is_model_combobox_placeholder",
    "_sanitize_model_combobox_value",
    "lru_config_key",
    "next_lru_list",
    "update_lru_history",
]

_MODEL_COMBO_PLACEHOLDER_MSGIDS = (
    "(Enter API Key to load models)",
    "(Connection failed)",
    "(No image models on this endpoint)",
    "(Default for current endpoint)",
)


def _is_model_combobox_placeholder(val: str) -> bool:
    text = str(val or "").strip()
    if not text:
        return False

    for msgid in _MODEL_COMBO_PLACEHOLDER_MSGIDS:
        if text == msgid or text == _(msgid):
            return True
    return False


def _sanitize_model_combobox_value(val: str) -> str:
    """Drop Settings combobox placeholder strings; they are not real model ids."""
    cleaned = str(val or "").strip()
    return "" if _is_model_combobox_placeholder(cleaned) else cleaned


def lru_config_key(lru_key: str, endpoint: str) -> str:
    """Config key for an LRU list. Endpoint-scoped lists are ``name@url``."""
    return f"{lru_key}@{endpoint}" if endpoint else lru_key


def next_lru_list(current: Any, val: Any, max_items: int) -> list[str] | None:
    """LRU list after prepending *val*, or None when that would not be a write.

    Blank values are not entries. A value that is already the first item is
    not a write. *current* may be a non-list (a missing key read as something
    else); that still becomes a one-item list, matching the old
    ``update_lru_history`` compare.
    """
    val_str = str(val).strip()
    if not val_str:
        return None
    # LRU entries are model id strings; get_config is JSON-shaped so normalize explicitly.
    lru: list[str] = [str(item) for item in current] if isinstance(current, list) else []
    # Already at the head: a set_config here would rewrite the file and emit
    # config:changed for a list the UI already shows first.
    if lru and lru[0] == val_str:
        return None
    if val_str in lru:
        lru.remove(val_str)
    lru.insert(0, val_str)
    new_lru = lru[:max_items]
    if isinstance(current, list) and current == new_lru:
        return None
    return new_lru


def update_lru_history(val: Any, lru_key: str, endpoint: str, max_items: int = LRU_MAX_ITEMS) -> None:
    """Prepend *val* to an endpoint-scoped LRU list in writeragent.json.

    No write when the value is blank or already the first item. Settings OK
    and the sidebar model sync do not call this once per key: they merge
    ``next_lru_list`` into the same ``set_configs`` as the field change.
    """
    scoped_key = lru_config_key(lru_key, endpoint)
    new_lru = next_lru_list(get_config(scoped_key), val, max_items)
    if new_lru is None:
        return
    set_config(scoped_key, new_lru)
