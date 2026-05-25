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



def ensure_plugin_on_path(
    __file__: str,
    levels_up: int = 3,
    also_add_lib: bool = False,
    also_add_contrib: bool = False,
    also_add_plugin_dir: bool = False,
    also_add_vendor: bool = False,
) -> str:
    """
    Walk up from the caller's __file__ and add the extension root on sys.path.

    This must be called **very early** (ideally as one of the first statements
    in the module, before any `from plugin.xxx import ...`) in any file that
    can be loaded directly by LibreOffice as a UNO component (XJob, XProofreader,
    Calc add-ins, etc.).

    Why this exists:
        unopkg / LibreOffice's Python UNO loader does not reliably put the
        extension root on sys.path for standalone .py components. Without this,
        "import plugin.foo" fails with ModuleNotFoundError.

    Common usage patterns:
        - Most plugin/ submodules: levels_up=3
        - Deeply nested components (e.g. writer/locale/): levels_up=4
        - Main entry points that need vendor/lib paths: also_add_vendor=True,
          also_add_lib=True, also_add_plugin_dir=True

    Args:
        __file__: Pass the module's own __file__ (the standard pattern).
        levels_up: Directory levels to ascend to reach the extension root.
                   3 is correct for most files under plugin/<something>/.
                   4 is required for some deeply nested standalone components
                   (e.g. the AI grammar proofreader under writer/locale/).
        also_add_lib: Add <ext_root>/plugin/lib (vendored pure-Python packages
                      used in the packaged OXT).
        also_add_contrib: Add <ext_root>/plugin/contrib (rarely needed for
                          early bootstrap; prefer runtime resolution when possible).
        also_add_plugin_dir: Add the plugin/ directory itself (useful for some
                             entry points that import things relative to it).
        also_add_vendor: Add the root-level vendor/ directory
                         (<ext_root>/vendor). Used in development for cross-platform
                         wheels (sounddevice, cffi, etc.).

    Returns:
        The extension root path that is now on sys.path.
    """
    this_file = os.path.abspath(__file__)
    for _ in range(levels_up):
        this_file = os.path.dirname(this_file)
    ext_root = this_file

    if ext_root not in sys.path:
        sys.path.insert(0, ext_root)

    if also_add_plugin_dir:
        plugin_dir = os.path.join(ext_root, "plugin")
        if os.path.isdir(plugin_dir) and plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)

    if also_add_lib:
        lib_dir = os.path.join(ext_root, "plugin", "lib")
        if os.path.isdir(lib_dir) and lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)

    if also_add_contrib:
        contrib_dir = os.path.join(ext_root, "plugin", "contrib")
        if os.path.isdir(contrib_dir) and contrib_dir not in sys.path:
            sys.path.insert(0, contrib_dir)

    if also_add_vendor:
        vendor_dir = os.path.join(ext_root, "vendor")
        if os.path.isdir(vendor_dir) and vendor_dir not in sys.path:
            sys.path.insert(0, vendor_dir)

    return ext_root
