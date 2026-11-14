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
"""Unit tests for shared specialized Writer helpers."""

from __future__ import annotations

from plugin.writer.specialized_base import index_error


def test_index_error_valid_index() -> None:
    assert index_error(0, 3, "footnote") is None
    assert index_error(1, 3, "footnote") is None
    assert index_error(2, 3, "footnote") is None


def test_index_error_out_of_bounds() -> None:
    assert index_error(-1, 3, "footnote") == "Invalid index -1. The document has 3 footnote(s)."
    assert index_error(3, 3, "footnote") == "Invalid index 3. The document has 3 footnote(s)."
    assert index_error(10, 3, "endnote") == "Invalid index 10. The document has 3 endnote(s)."
    assert index_error(0, 0, "footnote") == "Invalid index 0. The document has 0 footnote(s)."

