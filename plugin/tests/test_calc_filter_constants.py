# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.framework.calc_filter_constants import (
    FILTER_OPERATOR2_LABELS,
    filter_operator2_code_to_name,
    filter_operator2_name_to_code,
)


def test_filter_operator2_round_trip_known_codes():
    assert filter_operator2_code_to_name(0) == "EMPTY"
    assert filter_operator2_code_to_name(12) == "CONTAINS"
    assert filter_operator2_code_to_name(17) == "DOES_NOT_END_WITH"
    assert filter_operator2_code_to_name(99) == "99"

    assert filter_operator2_name_to_code("contains") == 12
    assert filter_operator2_name_to_code("BEGINS_WITH") == 14
    assert filter_operator2_name_to_code("bogus") is None


def test_filter_operator2_labels_complete():
    assert len(FILTER_OPERATOR2_LABELS) == 18
    assert FILTER_OPERATOR2_LABELS[0] == "EMPTY"
    assert FILTER_OPERATOR2_LABELS[-1] == "DOES_NOT_END_WITH"
