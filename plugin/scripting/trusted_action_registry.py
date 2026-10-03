# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Declarative registry for worker harness run_trusted_action dispatch.

These reviewed modules bypass the AST sandbox and run with full venv privileges.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from plugin.framework.deal_shim import DEAL_MAX_TOKEN, deal, str_bounded

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class TrustedActionWiring:
    """One trusted-action domain routed inside the venv worker."""

    domain: str
    handler: str
    supports_heartbeat: bool = False

    def dispatch(self, data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
        parts = self.handler.rsplit(":", 1)
        # What was wrong: a handler string with no colon made rsplit return
        # one element, and the unpack raised ValueError with no context.
        # Why this works: the wiring contract is module:attr; anything else
        # fails here with that shape instead of a bare unpack error.
        if len(parts) != 2:
            raise ValueError(
                f"Trusted action handler must be 'module:attr', got {self.handler!r}"
            )
        mod_name, attr_name = parts
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, attr_name)
        # supports_heartbeat is the contract that the handler accepts
        # heartbeat_fn. Passing it anyway raises TypeError for a handler
        # that does not take that keyword (and does not take **kwargs).
        if self.supports_heartbeat:
            return fn(data, heartbeat_fn=heartbeat_fn)
        return fn(data)


_TRUSTED_ACTION_WIRING: tuple[TrustedActionWiring, ...] = (
    TrustedActionWiring("units", "plugin.scripting.venv.trusted_dispatch:dispatch_units"),
    TrustedActionWiring("symbolic", "plugin.scripting.venv.trusted_dispatch:dispatch_symbolic"),
    TrustedActionWiring("math", "plugin.scripting.venv.trusted_dispatch:dispatch_symbolic"),
    TrustedActionWiring("viz", "plugin.scripting.venv.trusted_dispatch:dispatch_viz"),
    TrustedActionWiring("analysis", "plugin.scripting.venv.trusted_dispatch:dispatch_analysis"),
    TrustedActionWiring("forecast", "plugin.scripting.venv.trusted_dispatch:dispatch_forecast"),
    TrustedActionWiring("optimize", "plugin.scripting.venv.trusted_dispatch:dispatch_optimize"),
    TrustedActionWiring("quant", "plugin.scripting.venv.trusted_dispatch:dispatch_quant"),
    TrustedActionWiring("text", "plugin.scripting.venv.trusted_dispatch:dispatch_text"),
    TrustedActionWiring("vision", "plugin.scripting.venv.trusted_dispatch:dispatch_vision"),
    TrustedActionWiring("sql", "plugin.scripting.venv.trusted_dispatch:dispatch_sql"),
    TrustedActionWiring("languagetool", "plugin.scripting.venv.trusted_dispatch:dispatch_languagetool"),
    TrustedActionWiring("vale", "plugin.scripting.venv.trusted_dispatch:dispatch_vale"),
    TrustedActionWiring("embedding", "plugin.scripting.venv.trusted_dispatch:dispatch_embedding"),
    TrustedActionWiring("langdetect", "plugin.scripting.venv.trusted_dispatch:dispatch_langdetect"),
    TrustedActionWiring(
        "embeddings_index",
        "plugin.embeddings.venv.embeddings_index_dispatch:dispatch_trusted",
        supports_heartbeat=True,
    ),
    TrustedActionWiring(
        "folder_fts",
        "plugin.embeddings.venv.folder_fts_dispatch:dispatch_trusted",
        supports_heartbeat=True,
    ),
)

_wiring_by_domain: dict[str, TrustedActionWiring] | None = None


@deal.pre(lambda domain: str_bounded(domain, DEAL_MAX_TOKEN))
@deal.post(lambda result: result is None or isinstance(result, TrustedActionWiring))
def get_trusted_action_wiring(domain: str) -> TrustedActionWiring | None:
    """Return wiring for *domain*, or None when unregistered."""
    global _wiring_by_domain
    if _wiring_by_domain is None:
        _wiring_by_domain = {w.domain: w for w in _TRUSTED_ACTION_WIRING}
    return _wiring_by_domain.get(str(domain or ""))
