"""Cell address processing helper functions.

Pure utility functions with no UNO dependency. Ported from
core/calc_address_utils.py for the plugin framework.
"""

import re


def column_to_index(col_str: str) -> int:
    """Convert column letter to 0-based index.

    Args:
        col_str: Column letter (e.g. "A", "AB").

    Returns:
        0-based column index.
    """
    result = 0
    for char in col_str.upper():
        result = result * 26 + (ord(char) - ord('A') + 1)
    return result - 1


def index_to_column(index: int) -> str:
    """Convert 0-based column index to letter notation.

    Args:
        index: 0-based column index.

    Returns:
        Column letter (e.g. "A", "AB").
    """
    result = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord('A') + remainder) + result
    return result


def parse_address(address: str) -> tuple[int, int]:
    """Convert cell address to column and row indices.

    Args:
        address: Cell address (e.g. "A1", "AB10").

    Returns:
        (column_index, row_index) tuple (0-based).

    Raises:
        ValueError: Invalid cell address.
    """
    address = address.strip().upper()
    match = re.match(r'^([A-Z]+)(\d+)$', address)
    if not match:
        raise ValueError(f"Invalid cell address: '{address}'")

    col_str = match.group(1)
    row_num = int(match.group(2))

    col_index = column_to_index(col_str)
    row_index = row_num - 1

    return col_index, row_index


def parse_range_string(range_str: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Convert cell range string to column/row indices.

    Args:
        range_str: Range string in "A1:D10" or "A1" format.

    Returns:
        ((start_col, start_row), (end_col, end_row)) tuple.
        Both tuples are the same for a single cell.

    Raises:
        ValueError: Invalid range format.
    """
    range_str = range_str.strip().upper()

    pattern = r'^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$'
    match = re.match(pattern, range_str)
    if not match:
        raise ValueError(f"Invalid cell range format: '{range_str}'")

    start_col = column_to_index(match.group(1))
    start_row = int(match.group(2)) - 1

    if match.group(3) is not None:
        end_col = column_to_index(match.group(3))
        end_row = int(match.group(4)) - 1
    else:
        end_col = start_col
        end_row = start_row

    return (start_col, start_row), (end_col, end_row)


def format_address(col: int, row: int) -> str:
    """Create cell address from column and row indices.

    Args:
        col: 0-based column index.
        row: 0-based row index.

    Returns:
        Cell address (e.g. "A1", "AB10").
    """
    return f"{index_to_column(col)}{row + 1}"
