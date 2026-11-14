# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""R1 (robustness): a successful edit result must carry the CURRENT review status per-call, so the
model is correct even if the initialize manual is stale (manual is sent once at connect; toggling
the mode later doesn't update it). No LibreOffice required."""
from unittest.mock import patch

from plugin.writer.edit_review import annotate_review_status
import pytest


def _annotate(mode, result):
    with patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value=mode):
        return annotate_review_status(result)


def test_off_mode_leaves_result_unchanged():
    out = _annotate("off", {"status": "ok", "message": "Replaced 1 occurrence."})
    assert "pending_review" not in out
    assert out["message"] == "Replaced 1 occurrence."


def test_record_mode_marks_pending():
    out = _annotate("record", {"status": "ok", "message": "Replaced 1 occurrence."})
    assert out["pending_review"] is True
    assert out["review_mode"] == "record"
    assert out["message"] != "Replaced 1 occurrence."


def test_wait_mode_also_annotated():
    out = _annotate("wait", {"status": "ok", "message": "x"})
    assert out["pending_review"] is True and out["review_mode"] == "wait"


@pytest.mark.parametrize(
    "value, value_2, value_3",
    [
        pytest.param("record", "error", "old_content not found", id="test_error_result_not_annotated"),
        pytest.param("nonsense", "ok", "x", id="test_unknown_mode_not_annotated"),
    ],
)
def test_error_result_not_annotated(value, value_2, value_3):
    out = _annotate(value, {"status": value_2, "message": value_3})
    assert "pending_review" not in out


