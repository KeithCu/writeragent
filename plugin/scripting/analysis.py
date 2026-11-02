# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Analysis helper templates and constants (host / LO process).

Compute is lazy-loaded from ``plugin.scripting.venv.analysis`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_registry import names

_ANALYSIS_VENV_EXPORTS = names("analysis") | {"QuickStats", "run_analysis"}

_ANALYSIS_COERCE_EXPORTS = frozenset({"CoerceResult", "coerce_to_dataframe", "grid_to_dataframe"})

install_lazy_exports(
    globals(),
    "analysis",
    _ANALYSIS_VENV_EXPORTS,
    extra={"coerce": _ANALYSIS_COERCE_EXPORTS},
)


# --- Templates ---


def get_analysis_script_templates() -> dict[str, str]:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("analysis").templates()


def parse_analysis_script_header(code: str) -> Any:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("analysis").parse_header(code)
