# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Doc package marker (LibrePy: doc_type, text_helpers, udprops, visual_helpers, compound_undo helpers only).

WriterAgent ``load_modules`` looks for a ``ModuleBase`` subclass on this
package. ``common_module.py`` is omitted from the LibrePy OXT, so the
import fails there and the package stays inert (no embeddings / chat tools).
"""

try:
    from .common_module import CommonModule as CommonModule
except ImportError:
    pass
