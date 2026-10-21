# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``calc``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

from . import calc_functions_a_c as _a_c
from . import calc_functions_d_h as _d_h
from . import calc_functions_i_m as _i_m
from . import calc_functions_n_s as _n_s
from . import calc_functions_t_z as _t_z
from .calc_functions_a_c import *  # noqa: F403
from .calc_functions_d_h import *  # noqa: F403
from .calc_functions_i_m import *  # noqa: F403
from .calc_functions_n_s import *  # noqa: F403
from .calc_functions_t_z import *  # noqa: F403

HELPER_NAMES = frozenset(_a_c.__all__) | frozenset(_d_h.__all__) | frozenset(_i_m.__all__) | frozenset(_n_s.__all__) | frozenset(_t_z.__all__)

__all__ = ["HELPER_NAMES", *sorted(HELPER_NAMES)]  # pyright: ignore[reportUnsupportedDunderAll]
