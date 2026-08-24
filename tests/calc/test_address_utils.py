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
    assert column_to_index("ZZZ") == 18277
    assert index_to_column(0) == "A"
    assert index_to_column(26) == "AA"
    assert index_to_column(18277) == "ZZZ"
    assert parse_address("A1") == (0, 0)
    assert parse_address("B10") == (1, 9)
    assert format_address(0, 0) == "A1"
    assert format_address(18277, 0) == "ZZZ1"

    # Round-trip
    for addr in ("A1", "B10", "Z1", "AA100", "ZZZ1"):
        col, row = parse_address(addr)
        assert format_address(col, row) == addr

    import pytest

    try:
        import deal
        pre_err = (ValueError, deal.PreContractError)
    except Exception:
        pre_err = (ValueError,)

    # Valid cases
    assert parse_address("AA100") == (26, 99)

    # ASCII invalid addresses raise ValueError directly
    for ascii_invalid in ("A0", "A00", "Invalid"):
        with pytest.raises(ValueError):
            parse_address(ascii_invalid)

    # Non-ASCII addresses raise ValueError (or PreContractError when deal pre is active)
    for non_ascii_invalid in ("A🯰", "Ａ１", "A١"):
        with pytest.raises(pre_err):
            parse_address(non_ascii_invalid)

    # 18278 is past ZZZ; @deal.pre raises when deal is installed (shim is a no-op in LO).
    with pytest.raises(pre_err):
        index_to_column(18278)
    with pytest.raises(pre_err):
        format_address(18278, 0)

    assert parse_range_string("A1:B2") == ((0, 0), (1, 1))
    assert parse_range_string("C3") == ((2, 2), (2, 2))

    # Invalid range strings raise ValueError
    for invalid_rng in ("A1:Z", "A1:B0", "A0:B1"):
        with pytest.raises(ValueError):
            parse_range_string(invalid_rng)

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
