# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import options dialog."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.import_dialog import result_message


def test_result_message():
    success_title, success_msg = result_message({"summary": "s", "failed_verifications": []})
    assert success_title == "Success"
    assert "Conversion completed successfully!" in success_msg

    mismatch_title, mismatch_msg = result_message({
        "summary": "s",
        "failed_verifications": [{"address": "B2", "message": "expected 10 got 20"}],
    })
    assert mismatch_title == "Conversion finished with mismatches"
    assert "1 cell(s) did not verify." in mismatch_msg
    assert "B2: expected 10 got 20" in mismatch_msg
