# SPDX-License-Identifier: GPL-3.0-or-later
"""Binding-only Excel ``xl()`` data bridge for ``=PY`` sandboxes.

Looks up formula range bindings already injected as ``ranges`` (and polymorphic
``data``). No sheet RPC, no co-volatility, no dynamic ``xl(variable)`` / f-strings.

Refs are ``%Pn%`` strings (``%P2%`` = first binding). Header modes match the
Excel→DAG converter:
``True`` → ``to_pandas()``, ``False`` → ``to_pandas(header_row=None)``, omitted → bare ``CalcRange``.
"""

from __future__ import annotations

import re
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, deal

_P_TOKEN_RE = re.compile(r"^%P(\d+)%$", re.IGNORECASE)

# Sentinel so callers can distinguish omitted headers= from headers=False.
_HEADERS_OMIT = object()


_FIRST_BINDING = 2


@deal.pre(
    lambda ranges: ranges is None
    or (isinstance(ranges, (list, tuple)) and len(ranges) <= DEAL_MAX_SHAPE_DIM)
)
def make_xl(ranges: tuple[Any, ...] | list[Any] | None) -> Any:
    """Return an Excel-shaped ``xl(ref, headers=…)`` closed over *ranges*."""
    # Avoid `ranges or ()` — CrossHair returns SymbolicBool from __bool__ on empty tuples.
    bound = tuple(ranges) if ranges is not None else ()

    def xl(ref: Any, headers: Any = _HEADERS_OMIT) -> Any:
        if not isinstance(ref, str):
            raise ValueError(
                "xl() only accepts %Pn% binding strings (e.g. '%P2%'); "
                f"got {type(ref).__name__}"
            )
        m = _P_TOKEN_RE.match(ref.strip())
        if not m:
            raise ValueError(
                "xl() only resolves formula bindings like '%P2%'; "
                f"got {ref!r} (no live sheet reads)"
            )
        ref_num = int(m.group(1))
        idx = ref_num - _FIRST_BINDING
        if idx < 0 or idx >= len(bound):
            raise ValueError(
                f"xl({ref!r}) has no matching data binding "
                f"(ref {ref_num}, have {len(bound)} ranges)"
            )
        rng = bound[idx]
        if headers is _HEADERS_OMIT:
            return rng
        if headers is True:
            return rng.to_pandas()
        if headers is False:
            return rng.to_pandas(header_row=None)
        raise ValueError(f"xl() headers must be True or False; got {headers!r}")

    return xl
