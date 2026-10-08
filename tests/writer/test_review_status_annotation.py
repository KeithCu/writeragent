# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""R1 (robustness): a successful edit result must carry the CURRENT review status per-call, so the
model is correct even if the initialize manual is stale (manual is sent once at connect; toggling
the mode later doesn't update it). No LibreOffice required."""
from unittest.mock import patch


from plugin.writer.content import ApplyDocumentContent
import pytest

_tool = ApplyDocumentContent()


def _annotate(mode, result):
    with patch("plugin.writer.content.get_agent_edit_review_mode", return_value=mode):
        return _tool._annotate_review_status(object(), result)


def test_off_mode_leaves_result_unchanged():
    out = _annotate("off", {"status": "ok", "message": "Replaced 1 occurrence."})
    assert "pending_review" not in out
    assert "tracked change" not in out["message"]


def test_record_mode_marks_pending_and_no_outcome_notice():
    out = _annotate("record", {"status": "ok", "message": "Replaced 1 occurrence."})
    assert out["pending_review"] is True
    assert out["review_mode"] == "record"
    m = out["message"].lower()
    assert "tracked change" in m and "do not accept or reject" in m
    assert "not be notified" in m


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


