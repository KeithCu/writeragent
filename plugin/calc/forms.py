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
subclasses ``ToolWriterSpecialBase`` and ``ToolCalcSpecialBase``; union ``uno_services`` on concrete tools). Re-export
with Writer-prefixed aliases for clarity (cf. ``DrawCreateShape`` in writer/shapes).
"""

from plugin.writer.specialized.forms import CreateForm as WriterCreateForm
from plugin.writer.specialized.forms import CreateFormControl as WriterCreateFormControl
from plugin.writer.specialized.forms import DeleteFormControl as WriterDeleteFormControl
from plugin.writer.specialized.forms import EditFormControl as WriterEditFormControl
from plugin.writer.specialized.forms import GenerateForm as WriterGenerateForm
from plugin.writer.specialized.forms import ListFormControls as WriterListFormControls

__all__ = ["WriterCreateForm", "WriterCreateFormControl", "WriterDeleteFormControl", "WriterEditFormControl", "WriterGenerateForm", "WriterListFormControls"]
