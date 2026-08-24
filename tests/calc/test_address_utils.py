# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.



def test_address_utils():
    from plugin.calc.address_utils import (
        column_to_index,
        format_address,
        index_to_column,
        parse_address,
        parse_range_string,
        split_sheet_prefix,
    )

    assert column_to_index("A") == 0
    assert column_to_index("AA") == 26
    assert index_to_column(0) == "A"
    assert index_to_column(26) == "AA"
    assert parse_address("A1") == (0, 0)
    assert parse_address("B10") == (1, 9)
    assert format_address(0, 0) == "A1"

    # Round-trip
    for addr in ("A1", "B10", "Z1", "AA100"):
        col, row = parse_address(addr)
        assert format_address(col, row) == addr

    try:
        parse_address("Invalid")
        assert False, "Expected ValueError for 'Invalid'"
    except ValueError:
        pass

    # Non-ASCII digits and row 0 must be rejected or raise ValueError
    try:
        parse_address("A0")
        assert False, "Expected ValueError for 'A0'"
    except ValueError:
        pass

    try:
        parse_address("A🯰")
        assert False, "Expected ValueError for 'A🯰'"
    except (ValueError, Exception):
        pass

    assert parse_range_string("A1:B2") == ((0, 0), (1, 1))
    assert parse_range_string("C3") == ((2, 2), (2, 2))

    try:
        parse_range_string("A1:Z")
        assert False, "Expected ValueError for 'A1:Z'"
    except ValueError:
        pass

    # Sheet-qualified refs: split keeps sheet case; parse rejects leftover prefixes.
    assert split_sheet_prefix("Sheet1.A1:C5") == ("Sheet1", "A1:C5")
    assert split_sheet_prefix("'Data Sheet'!B2") == ("Data Sheet", "B2")
    assert split_sheet_prefix("Summary.D4:D6") == ("Summary", "D4:D6")
    assert split_sheet_prefix("A1:C5") == (None, "A1:C5")
    assert parse_range_string(split_sheet_prefix("Sheet1.A1:C5")[1]) == ((0, 0), (2, 4))

    try:
        parse_address("Sheet1.A1")
        assert False, "Expected ValueError for sheet-qualified address"
    except ValueError as e:
        assert "names a sheet" in str(e)
        assert "Summary" not in str(e)  # must not uppercase unrelated names
        assert "Sheet1" in str(e)

    try:
        parse_range_string("'Sheet One'!A1:B2")
        assert False, "Expected ValueError for sheet-qualified range"
    except ValueError as e:
        assert "Sheet One" in str(e)
