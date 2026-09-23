# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer module — tools for Writer document manipulation."""

import sys
from typing import Any

from plugin.framework.module_base import ModuleBase

# UNO / tool-registration imports: skipped under CrossHair so FQN cover of pure
# submodules (word_diff_split, xhtml_style_postprocess) does not pull format.py's
# tempfile.gettempdir() probe (auditwall SideEffectDetected). initialize() loads them.
if "crosshair" not in sys.modules:
    from . import tree, proximity  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from . import content, selection, styles, tracking, page, search, structural, outline, navigation, target_resolver, get_image  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from .specialized import bookmarks  # noqa: F401  # pyright: ignore[reportUnusedImport]
    # mock_domains: disable tools by wrapping class block in ''' in specialized/mock_domains.py
    from .specialized import forms, charts, comments, shapes, indexes, textframes, fields, embedded, mail_merge, mock_domains  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from .locale import linguistic_index  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from .locale import ai_grammar_proofreader, grammar_proofread_locale, grammar_work_queue  # noqa: F401  # pyright: ignore[reportUnusedImport]


class WriterModule(ModuleBase):
    """Registers Writer tools for outline, content, comments, styles, etc."""

    def initialize(self, services: Any) -> None:
        self.services = services

        # Same load order as module import (needed when CrossHair skipped the eager block).
        from . import tree, proximity
        from . import content, selection, styles, tracking, page, search, structural, outline, navigation, target_resolver, get_image  # noqa: F401  # pyright: ignore[reportUnusedImport]
        from .specialized import bookmarks
        from .specialized import forms, charts, comments, shapes, indexes, textframes, fields, embedded, mail_merge, mock_domains  # noqa: F401  # pyright: ignore[reportUnusedImport]
        from .locale import linguistic_index
        from .locale import ai_grammar_proofreader, grammar_proofread_locale, grammar_work_queue  # noqa: F401  # pyright: ignore[reportUnusedImport]

        # Order matters: tree needs bookmarks, proximity/index need tree
        for module in (bookmarks, tree, proximity, linguistic_index):
            services.auto_discover(module)

        # Register tools automatically for the entire package
        services.tools.auto_discover_package(__name__)
        # Register tools in subpackages
        services.tools.auto_discover_package(f"{__name__}.locale")
        services.tools.auto_discover_package(f"{__name__}.math")
        services.tools.auto_discover_package(f"{__name__}.images")
        services.tools.auto_discover_package(f"{__name__}.specialized")
