# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from tests.calc.serialization_cases import all_serialization_cases


def test_case_ids_unique():
    ids = [c.id for c in all_serialization_cases()]
    assert len(ids) == len(set(ids))
