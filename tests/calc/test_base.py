"""Tests for shared Calc tool schema constants."""

from unittest.mock import MagicMock

from plugin.calc.base import CALC_SHEET_PREFIX_NOTE
from plugin.framework.tool import ToolRegistry


def test_calc_range_and_cell_params_state_the_sheet_prefix() -> None:
    """Every Calc array `range` param and cell-comment `cell` param includes CALC_SHEET_PREFIX_NOTE."""
    registry = ToolRegistry(MagicMock())
    registry.auto_discover_package("plugin.calc")
    tools = registry.get_tools(filter_doc_type=False, exclude_tiers=())

    matched: list[str] = []
    for tool in tools:
        if not type(tool).__module__.startswith("plugin.calc."):
            continue
        props = (tool.parameters or {}).get("properties") or {}
        range_prop = props.get("range")
        if isinstance(range_prop, dict) and range_prop.get("type") == "array":
            assert CALC_SHEET_PREFIX_NOTE in range_prop.get("description", ""), (
                f"{tool.name}.range missing CALC_SHEET_PREFIX_NOTE"
            )
            matched.append(f"{tool.name}.range")
        if tool.name in {"cell_comment_add", "cell_comment_delete"}:
            cell_prop = props.get("cell") or {}
            assert CALC_SHEET_PREFIX_NOTE in cell_prop.get("description", ""), (
                f"{tool.name}.cell missing CALC_SHEET_PREFIX_NOTE"
            )
            matched.append(f"{tool.name}.cell")

    assert len(matched) == 15
