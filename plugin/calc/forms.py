# WriterAgent - AI Writing Assistant for LibreOffice
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
"""Calc package anchor for form tools.

Implementation and registration live in ``plugin.writer.specialized.forms`` (``ToolWriterFormBase``
subclasses ``ToolWriterSpecialBase`` and ``ToolCalcSpecialBase``; union ``uno_services`` on concrete tools).
Imported for side effects so the form tool classes are loaded when ``CalcModule.initialize`` walks subclasses.
"""

import plugin.writer.specialized.forms  # noqa: F401  # pyright: ignore[reportUnusedImport]  # load form tool classes for subclass catalog

