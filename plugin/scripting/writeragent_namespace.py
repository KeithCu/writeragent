# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Slim-bundle stand-in for top-level ``writeragent`` when ``writeragent_api`` is not shipped.

LibrePy omits the generated tool-proxy module; ``AliasImporter`` loads this module
instead so ``from writeragent.scripting.* import ...`` can resolve child packages
via the existing writeragent→plugin alias. ``wa.scripts`` / ``wa.doc`` are attached
at execute time (``named_scripts.attach_named_script_libraries``).

Outside a script, wa.<domain> raises an AttributeError that names wa.scripts / wa.doc instead of this module.
"""

from typing import Any

__all__ = ("scripts", "doc")  # noqa: F822


def __getattr__(name: str) -> Any:
    # Dunder lookups must stay standard so introspection and import machinery work.
    if name.startswith("__"):
        raise AttributeError(name)
    if name in __all__:
        raise AttributeError(f"writeragent.{name} is only available while a Python script is running.")
    raise AttributeError(
        f"writeragent.{name} is not available in this build. "
        "Document tool APIs require WriterAgent; use wa.scripts / wa.doc for named scripts."
    )
