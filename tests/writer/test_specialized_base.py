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

from plugin.writer.specialized.textframes import FrameSetProperties
from plugin.writer.specialized_base import (
    INSERT_TARGETS,
    DelegateToSpecializedWriter,
    ToolWriterTextFramesBase,
    enum_param,
    index_error,
    target_params,
)


def test_insert_targets_list() -> None:
    assert INSERT_TARGETS == ["beginning", "end", "selection", "full_document", "search"]


def test_enum_param() -> None:
    schema = enum_param(["a", "b"], "Choose a or b.")
    assert schema == {
        "type": "string",
        "enum": ["a", "b"],
        "description": "Choose a or b.",
    }


def test_enum_param_extra_kwargs() -> None:
    schema = enum_param(["x", "y"], "Choice.", default="x")
    assert schema == {
        "type": "string",
        "enum": ["x", "y"],
        "description": "Choice.",
        "default": "x",
    }


def test_target_params() -> None:
    props = target_params("the table")
    assert "target" in props
    assert "old_content" in props
    assert props["target"] == {
        "type": "string",
        "enum": INSERT_TARGETS,
        "description": "Where to insert the table.",
    }
    assert props["old_content"] == {
        "type": "string",
        "description": "Text to find and replace if target = 'search'.",
    }


def test_target_params_extra_kwargs() -> None:
    props = target_params("the field", default="end")
    assert props["target"]["default"] == "end"


def test_index_error_valid_index() -> None:
    assert index_error(0, 3, "footnote") is None
    assert index_error(1, 3, "footnote") is None
    assert index_error(2, 3, "footnote") is None


def test_index_error_out_of_bounds() -> None:
    assert index_error(-1, 3, "footnote") == "Invalid index -1. The document has 3 footnote(s)."
    assert index_error(3, 3, "footnote") == "Invalid index 3. The document has 3 footnote(s)."
    assert index_error(10, 3, "endnote") == "Invalid index 10. The document has 3 endnote(s)."
    assert index_error(0, 0, "footnote") == "Invalid index 0. The document has 0 footnote(s)."


def test_textframes_contract_matches_implemented_tools() -> None:
    desc = (ToolWriterTextFramesBase.specialized_domain_description or "").lower()
    assert "content" not in desc
    assert "list" in desc and "resize" in desc
    assert "reposition" not in (FrameSetProperties.description or "").lower()


def test_writer_gateway_description_has_no_hardcoded_domain_list() -> None:
    assert "Also:" not in DelegateToSpecializedWriter.description

