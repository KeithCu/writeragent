# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass-criteria checks for the headless N-document proof. No soffice, no API."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from prove_lo_multi_doc import (  # noqa: E402
    Span,
    WorkerResult,
    _tokens,
    doc_map_failures,
    document_isolation_failures,
    main,
    sleep_off_lo_thread_failures,
    sleep_overlap_failures,
    uno_serial_failures,
)


def test_sleeps_overlap_only_when_every_start_is_before_every_other_end() -> None:
    overlapped = [
        Span("a", 0.0, 0.5, 1),
        Span("b", 0.1, 0.6, 2),
    ]
    assert sleep_overlap_failures(overlapped) == []
    # Touching at the endpoint is not an overlap (start is not before the other end).
    serial = [
        Span("a", 0.0, 0.5, 1),
        Span("b", 0.5, 1.0, 2),
    ]
    assert sleep_overlap_failures(serial)
    assert sleep_overlap_failures([Span("only", 0.0, 0.5, 1)])


def test_uno_sections_must_be_serial_on_the_lo_thread() -> None:
    lo_ident = 7
    serial = [
        Span("write-a", 0.0, 1.0, lo_ident),
        Span("write-b", 1.0, 1.4, lo_ident),
    ]
    assert uno_serial_failures(serial, lo_ident) == []
    overlapped = [
        Span("write-a", 0.0, 1.0, lo_ident),
        Span("write-b", 0.4, 1.2, lo_ident),
    ]
    assert any("overlap" in msg for msg in uno_serial_failures(overlapped, lo_ident))
    off_thread = [
        Span("write-a", 0.0, 1.0, 3),
        Span("write-b", 1.0, 1.4, 3),
    ]
    assert any("_lo_thread" in msg for msg in uno_serial_failures(off_thread, lo_ident))


def test_documents_keep_only_their_own_tokens() -> None:
    token_a = _tokens(0)
    token_b = _tokens(1)
    clean = [
        WorkerResult(0, 10, token_a[0], token_a[1], token_a[0] + "\n" + token_a[1]),
        WorkerResult(1, 11, token_b[0], token_b[1], token_b[0] + "\n" + token_b[1] + "\n"),
    ]
    assert document_isolation_failures(clean) == []
    crossed = [
        WorkerResult(0, 10, token_a[0], token_a[1], token_a[0] + "\n" + token_b[0]),
        WorkerResult(1, 11, token_b[0], token_b[1], token_b[0] + "\n" + token_b[1]),
    ]
    messages = document_isolation_failures(crossed)
    assert messages
    assert any("cross-talk" in msg or "!=" in msg for msg in messages)


def test_doc_map_requires_one_slot_per_worker() -> None:
    assert doc_map_failures(2, (10, 11), {10, 11}, 2) == []
    assert doc_map_failures(1, (10,), {10, 11}, 2)


def test_sleep_must_stay_off_the_lo_thread() -> None:
    lo_ident = 7
    ok = [Span("w0-sleep", 0.0, 0.5, 1), Span("w1-sleep", 0.0, 0.5, 2)]
    assert sleep_off_lo_thread_failures(ok, lo_ident) == []
    on_lo = [Span("w0-sleep", 0.0, 0.5, lo_ident), Span("w1-sleep", 0.0, 0.5, 2)]
    assert any("outside LOBackend.call" in msg for msg in sleep_off_lo_thread_failures(on_lo, lo_ident))


def test_main_rejects_bad_n_and_testing_mode_without_starting_soffice(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("LOBackend.start must not run")

    monkeypatch.setattr("prove_lo_multi_doc.LOBackend.start", boom)
    assert main(["--n", "1"]) == 1
    assert main(["--n", "6"]) == 1
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    assert main(["--n", "2"]) == 1
