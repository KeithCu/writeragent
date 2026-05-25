# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Helpers for Python modules loaded directly by LibreOffice as UNO components.

When LibreOffice loads a .py file registered as a UNO service (XProofreader,
XJob, etc.), it often does so with a very limited sys.path. Code in those files
must manually ensure the extension root is on sys.path so that normal
`import plugin.xxx` statements work.

This module centralizes the fragile path-walking logic that was previously
duplicated (with slight variations) in several places.
"""

import os
import sys
from typing import Optional


def ensure_plugin_on_path(
    __file__: str,
    levels_up: int = 3,
    also_add_lib: bool = False,
    also_add_contrib: bool = False,
) -> str:
    """
    Walk up from the caller's __file__ and add the extension root to sys.path.

    This must be called **very early** (ideally as one of the first statements
    in the module, before any `from plugin.xxx import ...`) in any file that
    can be loaded directly by LibreOffice as a UNO component.

    Why this exists:
        unopkg / LibreOffice's Python UNO loader does not reliably put the
        extension root on sys.path for standalone .py components. Without this,
        "import plugin.foo" fails with ModuleNotFoundError.

    Args:
        __file__: Pass the module's own __file__ (the standard pattern).
        levels_up: Directory levels to ascend.
                   3 is correct for most files under plugin/<subdir>/.
                   4 is required for some deeply nested standalone components
                   (e.g. the AI grammar proofreader under writer/locale/).
        also_add_lib: Add plugin/lib (vendored pure-Python packages). Needed by
                      components that use json_repair etc. without the full OXT
                      layout helping them.
        also_add_contrib: Add plugin/contrib (rarely needed).

    Returns:
        The extension root path that is now on sys.path.
    """
    this_file = os.path.abspath(__file__)
    for _ in range(levels_up):
        this_file = os.path.dirname(this_file)
    ext_root = this_file

    if ext_root not in sys.path:
        sys.path.insert(0, ext_root)

    if also_add_lib:
        lib_dir = os.path.join(ext_root, "plugin", "lib")
        if os.path.isdir(lib_dir) and lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)

    if also_add_contrib:
        contrib_dir = os.path.join(ext_root, "plugin", "contrib")
        if os.path.isdir(contrib_dir) and contrib_dir not in sys.path:
            sys.path.insert(0, contrib_dir)

    return ext_root
