# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass criteria for the N=2 flag pin sketch. No soffice, no API."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from prove_lo_flag_pin import (  # noqa: E402
    SHAPE_NAME,
    TOKEN_A,
    TOKEN_B,
    Span,
    b_write_during_a_sleep_failures,
    flag_document_failures,
    main,
    odt_contains_shape_name,
)
from prove_lo_multi_doc import Span as MultiSpan  # noqa: E402


def test_b_write_must_start_during_a_sleep() -> None:
    sleep = Span("A-sleep", 1.0, 1.8, 3)
    inside = Span("B-setString", 1.1, 1.12, 7)
    assert b_write_during_a_sleep_failures(sleep, inside) == []
    too_early = Span("B-setString", 0.9, 1.0, 7)
    assert b_write_during_a_sleep_failures(sleep, too_early)
    assert b_write_during_a_sleep_failures(None, inside)


def test_flag_documents_reject_cross_writes() -> None:
    assert (
        flag_document_failures(
            a_text=TOKEN_A,
            b_text=TOKEN_B + "\n",
            a_has_shape=True,
            b_has_shape=False,
        )
        == []
    )
    crossed = flag_document_failures(
        a_text=TOKEN_A,
        b_text=TOKEN_B,
        a_has_shape=False,
        b_has_shape=True,
    )
    assert any("A .odt" in msg for msg in crossed)
    assert any("B .odt" in msg for msg in crossed)
    text_cross = flag_document_failures(
        a_text=TOKEN_A + "\n" + TOKEN_B,
        b_text=TOKEN_A,
        a_has_shape=True,
        b_has_shape=False,
    )
    assert any("cross-write" in msg for msg in text_cross)


def test_odt_shape_name_reads_content_xml(tmp_path: Path) -> None:
    path = tmp_path / "sample.odt"
    xml = (
        '<?xml version="1.0"?>'
        '<office:document-content>'
        f'<draw:rect draw:name="{SHAPE_NAME}"/>'
        "</office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", xml)
    assert odt_contains_shape_name(str(path), SHAPE_NAME)
    assert not odt_contains_shape_name(str(path), "other-shape")


def test_main_rejects_testing_mode_without_starting_soffice(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("LOBackend.start must not run")

    monkeypatch.setattr("prove_lo_flag_pin.LOBackend.start", boom)
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    assert main() == 1


def test_span_type_matches_the_text_proof() -> None:
    """Overlap checks share the text proof's Span so UNO serial checks stay one helper."""
    assert Span is MultiSpan
