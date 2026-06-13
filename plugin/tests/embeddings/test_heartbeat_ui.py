# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Search dialog rebuild heartbeat UI."""

from __future__ import annotations

import time

from plugin.embeddings.embeddings_heartbeat import format_index_heartbeat_line, heartbeat_counts_from_payload


class MockModel:
    def __init__(self):
        self.Text = ""


class MockCtrl:
    def __init__(self):
        self._model = MockModel()

    def getModel(self):
        return self._model


def execute_on_main_thread(func):
    func()


def test_heartbeat_updates_results_ctrl(monkeypatch):
    results_ctrl = MockCtrl()
    hb_data: dict = {}

    def heartbeat_fn(payload: dict):
        file = payload.get("file")
        if not file:
            return
        phase = payload.get("phase")
        now = time.time()
        if phase == "extract":
            paragraphs, chunks = heartbeat_counts_from_payload(payload)
            hb_data[file] = {"start": now, "paragraphs": paragraphs, "chunks": chunks}
            return
        if phase in ("embed", "index", "delete"):
            info = hb_data.get(file)
            if info is None:
                return
            elapsed = now - info["start"]
            payload_paragraphs, payload_chunks = heartbeat_counts_from_payload(payload)
            paragraphs = payload_paragraphs or int(info.get("paragraphs") or 0)
            chunks = payload_chunks or int(info.get("chunks") or 0)
            line = format_index_heartbeat_line(
                str(file),
                paragraphs=paragraphs,
                chunks=chunks,
                elapsed_sec=elapsed,
            )

            def ui_update():
                existing = results_ctrl.getModel().Text
                new_text = (existing + "\n" if existing else "") + line
                results_ctrl.getModel().Text = new_text

            execute_on_main_thread(ui_update)
            del hb_data[file]

    start_time = 1000.0
    monkeypatch.setattr(time, "time", lambda: start_time)
    heartbeat_fn({"file": "test.txt", "phase": "extract", "mode": "cold"})

    monkeypatch.setattr(time, "time", lambda: start_time + 1.234)
    heartbeat_fn({"file": "test.txt", "phase": "embed", "paragraphs": 5, "upserted": 6})

    assert results_ctrl.getModel().Text == "test.txt: 5 paragraphs, 6 chunks, 1.23s"
